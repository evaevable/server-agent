"""规划器、Runbook 与 Agent 规划模式测试。"""

import json

import pytest

from server_agent.agent.loop import Agent
from server_agent.agent.planner import Plan, Planner
from server_agent.knowledge import Runbook, RunbookLibrary, get_library, parse_runbook, reset_library
from server_agent.llm import Message
from server_agent.llm.mock import MockLLM, text, tool_call
from server_agent.tools import registry
from server_agent.tools.registry import ToolRegistry

PLAN_JSON = json.dumps({
    "rationale": "先看整体资源，再定位到进程，最后给结论",
    "steps": [
        {"goal": "看 CPU 与内存使用率", "tool_hint": "cpu_memory_usage"},
        {"goal": "找出占用最高的进程", "tool_hint": "top_processes"},
        {"goal": "确认磁盘是否也是瓶颈", "tool_hint": "disk_usage"},
    ],
}, ensure_ascii=False)


def make_tools() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="cpu_memory_usage")
    def _cpu(interval: float = 0.1) -> dict:
        """看资源。"""
        return {"cpu_percent": 92, "load_avg": {"1m": 8.2}}

    @reg.tool(name="top_processes")
    def _top(limit: int = 3) -> dict:
        """看进程。"""
        return {"processes": [{"pid": 42, "name": "python", "cpu_percent": 780.0}]}

    return reg


# ---------- 规划器 ----------

def test_planner_parses_plan():
    plan = Planner(MockLLM(["x"])).parse(PLAN_JSON)
    assert plan is not None and len(plan.steps) == 3
    assert plan.steps[0].tool_hint == "cpu_memory_usage"
    assert plan.pending == 3 and "先看整体资源" in plan.rationale
    rendered = plan.render()
    assert "[当前计划]" in rendered and "1. [ ]" in rendered


def test_planner_parses_fenced_and_string_steps():
    plan = Planner(MockLLM(["x"])).parse(
        "```json\n" + json.dumps({"steps": ["看磁盘", {"goal": "看日志"}]}) + "\n```")
    assert [s.goal for s in plan.steps] == ["看磁盘", "看日志"]


@pytest.mark.parametrize("raw", ["完全不是 JSON", "{}", '{"steps": []}'])
def test_planner_returns_none_on_bad_output(raw):
    assert Planner(MockLLM(["x"])).parse(raw) is None


def test_plan_progress_and_failure_marks():
    plan = Plan(steps=[{"goal": "a"}, {"goal": "b"}]) if False else Planner(MockLLM(["x"])).parse(PLAN_JSON)
    plan.mark_next_done()
    assert [s.status for s in plan.steps][:2] == ["done", "pending"]
    plan.mark_last_failed()
    assert plan.steps[1].status == "failed" and plan.pending == 1   # 第一个未完成步骤被标记失败
    assert "! " in plan.render() or "[" + "!" + "]" in plan.render()


async def test_make_plan_falls_back_when_llm_fails():
    llm = MockLLM([])
    llm.echo = False
    assert await Planner(llm).make_plan("磁盘满了") is None       # LLMError 被吞掉，降级为无计划


# ---------- Runbook ----------

def test_parse_and_search_runbooks(tmp_path):
    (tmp_path / "disk.md").write_text("""---
name: disk-full
title: 磁盘满
symptoms: [磁盘, 满了, df]
---
# 步骤
1. df -h
""", encoding="utf-8")
    (tmp_path / "no-front.md").write_text("# 普通文档\n没有 frontmatter", encoding="utf-8")

    rb = parse_runbook(tmp_path / "disk.md")
    assert rb.name == "disk-full" and rb.matches("web-01 磁盘满了") == 2
    assert parse_runbook(tmp_path / "no-front.md") is None

    lib = RunbookLibrary().load_dir(tmp_path)
    assert lib.names() == ["disk-full"]
    assert lib.search("磁盘快满了怎么办")[0].name == "disk-full"
    assert lib.search("用户登录失败") == []
    assert lib.get("nope") is None
    assert lib.get("disk-full").to_dict(with_body=False) == {
        "name": "disk-full", "title": "磁盘满", "symptoms": ["磁盘", "满了", "df"],
        "path": str(tmp_path / "disk.md")}


def test_repo_runbooks_load():
    reset_library(None)          # 清掉缓存，避免受其它测试影响
    lib = get_library()
    assert set(lib.names()) >= {"disk-full", "cpu-high", "service-502", "port-conflict"}
    assert "README" not in lib.names()      # runbooks/README.md 没有 frontmatter，不能被当成手册
    hits = lib.search("nginx 起不来，端口被占用")
    assert hits and hits[0].name in ("service-502", "port-conflict")


async def test_runbook_tools(monkeypatch):
    lib = RunbookLibrary([
        Runbook(name="disk-full", title="磁盘满", symptoms=["磁盘", "df"], body="1. df -h"),
    ])
    monkeypatch.setattr("server_agent.tools.runbook_tools.get_library", lambda: lib)

    r = await registry.call("list_runbooks", {"question": "磁盘满了"})
    assert r.ok and r.data["count"] == 1 and "body" not in r.data["runbooks"][0]

    r = await registry.call("list_runbooks", {})
    assert r.ok and r.data["runbooks"][0]["name"] == "disk-full"

    r = await registry.call("load_runbook", {"name": "disk-full"})
    assert r.ok and r.data["body"].startswith("1. df -h")

    r = await registry.call("load_runbook", {"name": "nope"})
    assert not r.ok and "没有这本手册" in r.error


async def test_list_runbooks_when_empty(monkeypatch):
    monkeypatch.setattr("server_agent.tools.runbook_tools.get_library", lambda: RunbookLibrary())
    r = await registry.call("list_runbooks", {"question": "任意"})
    assert r.ok and r.data["count"] == 0 and "没有找到任何手册" in r.data["hint"]


# ---------- Agent 规划模式 ----------

async def test_agent_planning_emits_plan_event_and_marks_steps():
    llm = MockLLM([PLAN_JSON, tool_call("cpu_memory_usage", {}), tool_call("top_processes", {}),
                   "结论：python 进程占满 CPU"])
    agent = Agent(llm, make_tools(), stream=False, planning=True, timeout=99)
    events = [ev async for ev in agent.run("这台机器为什么卡")]

    kinds = [e.type for e in events]
    assert kinds[0] == "plan" and "start" in kinds
    plan_data = events[0].data["plan"]
    assert len(plan_data["steps"]) == 3 and plan_data["steps"][0]["goal"].startswith("看 CPU")

    assert agent.plan.steps[0].status == "done" and agent.plan.steps[1].status == "done"
    assert agent.plan.pending == 1
    assert "当前计划" in llm.calls[1]["messages"][0].content      # 计划写进了系统提示词
    assert events[-1].data["text"].startswith("结论")


async def test_agent_planning_marks_failure_when_tools_error():
    llm = MockLLM([PLAN_JSON, tool_call("missing_tool", {}), "我换个思路"])
    agent = Agent(llm, make_tools(), stream=False, planning=True, timeout=99)
    await agent.run_sync("查一下")
    assert agent.plan.steps[0].status == "failed"


async def test_agent_without_planning_has_no_plan_event():
    llm = MockLLM(["直接回答"])
    events = [ev async for ev in Agent(llm, make_tools(), stream=False, planning=False).run("你好")]
    assert "plan" not in [e.type for e in events]


async def test_agent_planning_survives_plan_parse_failure():
    """计划生成失败（模型给了散文）时，排查照常进行——计划是增强不是前置条件。"""
    llm = MockLLM(["这不是 JSON", "结论：一切正常"])
    agent = Agent(llm, make_tools(), stream=False, planning=True, timeout=99)
    events = [ev async for ev in agent.run("随便看看")]
    assert "plan" not in [e.type for e in events] and events[-1].data["text"].startswith("结论")
    assert agent.plan is None
