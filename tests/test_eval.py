"""第 15 章：Trace（耗时树）与评测体系测试。"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server_agent.agent.loop import Agent
from server_agent.config import Settings
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.server.app import create_app
from server_agent.tools.registry import ToolRegistry
from server_agent.tracing import Trace


def make_tools() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"partitions": [{"mount": "/", "percent": 95}]}

    return reg


# ---------- Trace ----------

async def test_trace_records_spans_and_summary(tmp_path):
    llm = MockLLM([tool_call("disk_usage", {}), "结论：磁盘 95%"])
    agent = Agent(llm, make_tools(), stream=False, report=False)
    agent.trace = Trace("run_test", path=tmp_path)
    events = [ev async for ev in agent.run("查磁盘")]

    trace = agent.trace
    names = [s.name for s in trace.spans]
    assert names[0] == "run" and "llm" in names and any(n.startswith("tool:") for n in names)
    summary = trace.summary()
    assert summary["spans"] == len(trace.spans) and summary["total_ms"] >= 0
    assert summary["llm_ms"] > 0
    assert events[-1].data["trace"]["spans"] == len(trace.spans)

    # 落盘：每行一个 span，可被 jq/脚本处理
    lines = (tmp_path / "run_test.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(trace.spans)
    first = json.loads(lines[0])
    assert {"run_id", "span_id", "name", "start", "duration_ms", "attrs"} <= set(first)


async def test_trace_ctx_marks_error_and_write_failure_is_ignored(tmp_path, monkeypatch):
    trace = Trace("run_x", path=tmp_path)
    with pytest.raises(ValueError):
        async with trace.span("boom"):
            raise ValueError("炸了")
    assert trace.spans[-1].attrs["error"] and "炸了" in trace.spans[-1].attrs["error"]

    def boom(*_a, **_k):
        raise OSError("disk full")

    bad = Trace("run_y", path=tmp_path / "sub")
    monkeypatch.setattr(Path, "open", boom)
    span = bad.start_span("llm")
    bad.end_span(span)          # 写盘失败不能抛出去
    assert bad.spans[-1].end is not None


def test_trace_can_be_disabled(monkeypatch):
    from server_agent.config import get_settings
    from server_agent.tracing import trace_dir_default

    get_settings.cache_clear()
    monkeypatch.setenv("SA_TRACE_ENABLED", "false")
    try:
        assert trace_dir_default() is None
    finally:
        get_settings.cache_clear()


# ---------- 评测 ----------

def test_cases_load_and_have_required_fields():
    from evals.runner import load_cases

    cases = load_cases("evals/cases")
    assert len(cases) >= 5
    for c in cases:
        assert c.get("id") and c.get("question") and c.get("mock") and c.get("expect")


def test_case_passes_when_expectations_met():
    from evals.runner import run_case

    case = {
        "id": "t1", "question": "磁盘满了吗", "scenario": "disk_full",
        "mock": [
            {"tool_call": {"name": "disk_usage", "arguments": {"path": "/"}}},
            {"report": {"summary": "盘满", "severity": "critical", "confidence": "high",
                        "findings": [{"claim": "c", "evidence": "percent=95"}],
                        "root_cause": "日志未轮转", "actions": [], "data_gaps": []}},
        ],
        "expect": {"stopped": "final", "max_steps": 3, "tools_called": ["disk_usage"],
                   "root_cause_contains": ["轮转"]},
    }
    result = asyncio.run(run_case(case))
    assert result.passed and result.steps == 2 and result.tool_calls == 1
    assert "disk_usage" in result.tools_used and result.tokens > 0


def test_case_fails_on_missing_tool_or_wrong_root_cause():
    from evals.runner import run_case

    case = {
        "id": "t2", "question": "磁盘满了吗", "scenario": "disk_full",
        "mock": [{"report": {"summary": "盘满", "severity": "critical", "confidence": "high",
                             "findings": [], "root_cause": "买磁盘", "actions": [], "data_gaps": []}}],
        "expect": {"tools_called": ["disk_usage"], "root_cause_contains": ["轮转"]},
    }
    result = asyncio.run(run_case(case))
    assert not result.passed
    assert any("没有调用期望的工具" in r for r in result.reasons)
    assert any("根因未包含" in r for r in result.reasons)


def test_write_action_is_denied_and_counted():
    """评测里没有真人审批：写操作必须被拒绝，并且被计入指标。"""
    from evals.runner import run_case
    from evals.stubs import eval_state, reset_eval_state

    reset_eval_state()
    case = {
        "id": "t3", "question": "帮我清理日志", "scenario": "disk_full",
        "mock": [
            {"tool_call": {"name": "clean_directory",
                           "arguments": {"path": "/var/log/nginx", "older_than_days": 7, "dry_run": False}}},
            {"report": {"summary": "清理", "severity": "warning", "confidence": "medium",
                        "findings": [], "root_cause": "日志多", "actions": [], "data_gaps": []}},
        ],
        "expect": {"min_denials": 1},
    }
    result = asyncio.run(run_case(case))
    assert result.passed and result.denials >= 1 and result.executed == []
    assert eval_state["executed"] == []


def test_approved_write_executes():
    from evals.runner import run_case
    from evals.stubs import eval_state, reset_eval_state

    reset_eval_state()
    case = {
        "id": "t4", "question": "帮我清理日志", "scenario": "disk_full",
        "mock": [
            {"tool_call": {"name": "clean_directory",
                           "arguments": {"path": "/var/log/nginx", "older_than_days": 7, "dry_run": False}}},
            {"report": {"summary": "已清理", "severity": "warning", "confidence": "medium",
                        "findings": [], "root_cause": "日志多", "actions": [], "data_gaps": []}},
        ],
        "expect": {"must_execute": ["clean:/var/log/nginx"], "min_denials": 0},
    }
    result = asyncio.run(run_case(case, approve=True))
    assert result.passed and result.executed == ["clean:/var/log/nginx"]


def test_repo_eval_suite_passes_and_renders_report():
    """仓库自带的评测集是回归基线，必须全绿。"""
    from evals.runner import load_cases, render_report, run_all

    cases = load_cases("evals/cases")
    results = asyncio.run(run_all(cases))
    report = render_report(results, title="单元测试评测")

    assert len(results) == len(cases)
    failed = [r for r in results if not r.passed]
    assert not failed, f"以下用例未通过：{[(r.case_id, r.reasons) for r in failed]}"
    assert "通过率" in report and "策略拒绝次数" in report


def test_planning_case_supports_plan_step():
    from evals.runner import run_case

    case = {
        "id": "t5", "question": "机器很卡", "scenario": "cpu_busy", "planning": True,
        "mock": [
            {"text": '{"rationale": "先看资源", "steps": [{"goal": "看 CPU", "tool_hint": "cpu_memory_usage"}]}'},
            {"tool_call": {"name": "cpu_memory_usage", "arguments": {"interval": 0.1}}},
            {"report": {"summary": "CPU 高", "severity": "warning", "confidence": "high",
                        "findings": [{"claim": "c", "evidence": "load=9.2"}],
                        "root_cause": "python 死循环", "actions": [], "data_gaps": []}},
        ],
        "expect": {"tools_called": ["cpu_memory_usage"], "root_cause_contains": ["python"]},
    }
    result = asyncio.run(run_case(case))
    assert result.passed


# ---------- Trace 与服务的集成 ----------

def test_agent_writes_trace_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SA_TRACE_DIR", str(tmp_path))
    from server_agent.config import get_settings

    get_settings.cache_clear()
    try:
        reg = ToolRegistry()

        @reg.tool(name="disk_usage")
        def _disk(path: str = "/") -> dict:
            """查磁盘。"""
            return {"partitions": [{"mount": "/", "percent": 95}]}

        agent = Agent(MockLLM([tool_call("disk_usage", {}), "结论"]), reg, stream=False, report=False)
        events = asyncio.run(_collect(agent))
        files = list(tmp_path.glob("*.jsonl"))
        assert files, "Trace 文件没有落盘"
        spans = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        assert {s["name"] for s in spans} >= {"run", "llm"}
        assert any(s["name"].startswith("tool:") for s in spans)
        assert events[-1].data["trace"]["total_ms"] >= 0
    finally:
        get_settings.cache_clear()


async def _collect(agent):
    return [ev async for ev in agent.run("查磁盘")]


def test_service_run_emits_end_with_trace_summary():
    def factory():
        return Agent(MockLLM([tool_call("disk_usage", {}), "结论"]), ToolRegistry(),
                     stream=False, report=False)

    app = create_app(Settings(memory_enabled=False, trace_enabled=False), agent_factory=factory,
                     tools_registry=ToolRegistry())
    with TestClient(app) as c:
        run_id = c.post("/api/runs", json={"input": "x"}).json()["id"]
        import time

        for _ in range(60):
            if c.get(f"/api/runs/{run_id}").json()["status"] != "running":
                break
            time.sleep(0.02)
        with c.stream("GET", f"/api/runs/{run_id}/events") as resp:
            text = "".join(resp.iter_text())
        assert "event: end" in text
