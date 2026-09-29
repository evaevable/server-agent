"""SQLite 存储与记忆工具测试。"""

import json

import pytest

from server_agent.memory import Recorder, Store, build_memory_context, reset_store
from server_agent.tools import registry


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_run_lifecycle_and_events(store):
    store.save_run_start("run_1", "磁盘为什么满了")
    store.append_event("run_1", 1, "start", {"input": "x"})
    store.append_event("run_1", 2, "tool_call", {"name": "disk_usage"})
    store.save_run_end("run_1", status="done", steps=2, tool_calls=1, tokens=123,
                       text="结论", report={"summary": "根分区 97%", "root_cause": "日志未轮转",
                                          "confidence": "medium"})
    run = store.get_run("run_1")
    assert run["status"] == "done" and run["tokens"] == 123
    assert run["report"]["root_cause"] == "日志未轮转"
    assert [e["type"] for e in store.get_events("run_1")] == ["start", "tool_call"]
    assert store.get_events("run_1", after_seq=1)[0]["seq"] == 2
    assert store.get_run("nope") is None


def test_list_runs_orders_newest_first_and_counts_events(store):
    for i in range(3):
        store.save_run_start(f"run_{i}", f"问题{i}")
        store.append_event(f"run_{i}", 1, "step", {"step": 1})
    runs = store.list_runs()
    assert [r["id"] for r in runs] == ["run_2", "run_1", "run_0"]
    assert runs[0]["event_count"] == 1


def test_facts_and_host_profile_merge(store):
    store.update_host_profile("web-01", {"os": "Linux", "role": "nginx"})
    store.update_host_profile("web-01", {"os": None, "owner": "张三"})   # None 不覆盖，其它字段合并
    profile = store.get_host_profile("web-01")
    assert profile == {"os": "Linux", "role": "nginx", "owner": "张三"}
    assert store.get_host_profile("nope") is None

    store.remember_fact("web-01", "nginx_log_dir", "/data/logs/nginx")
    store.remember_fact("web-01", "backup", "每天 3 点")
    facts = store.recall_facts("web-01")
    assert [f["key"] for f in facts] == ["backup", "nginx_log_dir"]   # 最新在前
    assert store.recall_facts("other") == []


def test_recent_incidents_and_memory_context(store):
    store.save_run_start("run_9", "web-01 磁盘满了")
    store.save_run_end("run_9", status="done", steps=3, tool_calls=2, tokens=900,
                       report={"summary": "根分区 92%", "root_cause": "日志未轮转", "confidence": "high"})
    incidents = store.recent_incidents(host="web-01")
    assert incidents and incidents[0]["root_cause"] == "日志未轮转"
    assert store.recent_incidents(host="db-01") == []

    store.remember_fact("web-01", "role", "nginx")
    text = build_memory_context(store, host="web-01")
    assert "[历史记忆]" in text and "日志未轮转" in text and "role = nginx" in text
    assert build_memory_context(store) is not None          # 不带 host 也能给最近结论
    assert build_memory_context(Store(":memory:")) is None  # 空库


def test_recorder_writes_events_and_result(store):
    from server_agent.agent.events import AgentResult, Event
    from server_agent.llm import Usage

    rec = Recorder(store, run_id="run_r", user_input="查磁盘")
    rec.event(Event("start", "run_r", 1, {"input": "查磁盘"}))
    rec.event(Event("end", "run_r", 2, {}))          # end 不入库（由 finish 单独写）
    rec.finish(AgentResult(text="结论", steps=2, tool_calls=1, usage=Usage(10, 20, 30),
                           report={"summary": "s", "confidence": "low"}))
    run = store.get_run("run_r")
    assert run["tokens"] == 30 and run["report"]["summary"] == "s"
    assert [e["type"] for e in store.get_events("run_r")] == ["start"]


def test_recorder_bind_late(store):
    rec = Recorder(store, pending_run_id="", user_input="q")
    assert rec.persisted is False
    rec.bind("run_late")
    assert rec.persisted is True and store.get_run("run_late")["input"] == "q"


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    s1 = Store(path)
    s1.save_run_start("run_p", "重启前的问题")
    s1.save_run_end("run_p", status="done", steps=1, tool_calls=0, tokens=5, text="答案")
    s1.close()

    s2 = Store(path)          # 模拟进程重启
    assert s2.get_run("run_p")["text"] == "答案"
    s2.close()


async def test_memory_tools_roundtrip(store, monkeypatch):
    monkeypatch.setattr("server_agent.tools.memory.get_store", lambda: store)
    r = await registry.call("remember_fact", {"host": "web-01", "key": "role", "value": "nginx"})
    assert r.ok and r.data["saved"] and r.data["total_facts"] == 1

    r = await registry.call("recall_host", {"host": "web-01"})
    assert r.ok and r.data["facts"][0]["key"] == "role"
    assert r.data["hint"] is None

    r = await registry.call("recall_host", {"host": "unknown-host"})
    assert r.ok and "第一次排查" in r.data["hint"]
    assert registry.get("remember_fact").risk == "low"      # 会写记忆，但不是系统写操作
    assert registry.get("recall_host").risk == "read"
