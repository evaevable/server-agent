"""提示词模板与结构化报告测试。"""

import json

import pytest

from server_agent.agent.loop import Agent
from server_agent.config import Settings
from server_agent.llm import Message
from server_agent.llm.mock import MockLLM, text, tool_call
from server_agent.prompts import (
    DEFAULT_VARIANT,
    DiagnosticReport,
    VARIANTS,
    extract_json,
    json_schema_text,
    load_template,
    parse_report,
    render_system_prompt,
    repair_prompt,
)
from server_agent.tools.registry import ToolRegistry

GOOD_REPORT = {
    "summary": "根分区使用率 97%",
    "severity": "critical",
    "findings": [{"claim": "根分区接近写满", "evidence": "/ 使用率 percent=97"}],
    "root_cause": "/var/log 未轮转",
    "confidence": "medium",
    "actions": [{"description": "查看 /var/log 下最大的文件", "risk": "read", "command": "du -sh /var/log/*"}],
    "data_gaps": ["无法确认 logrotate 是否被禁用"],
}


def make_tools() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"mount": path, "percent": 97}

    return reg


# ---------- 模板 ----------

def test_variants_and_templates_exist():
    assert DEFAULT_VARIANT == "sre" and set(VARIANTS) == {"sre", "plain"}
    for v in VARIANTS:
        tpl = load_template(v)
        assert "$report_schema" in tpl and "$tools" in tpl
    with pytest.raises(ValueError, match="未知的提示词变体"):
        load_template("nope")


def test_render_system_prompt_embeds_methodology_tools_and_schema():
    prompt = render_system_prompt("sre", tools=["disk_usage", "tail_file"], hostname="h1", os_name="Linux")
    assert "USE 方法" in prompt and "证据链" in prompt
    assert "disk_usage, tail_file" in prompt and "h1" in prompt and "Linux" in prompt
    assert '"root_cause"' in prompt and '"confidence"' in prompt          # schema 被内联
    for ph in ("$hostname", "$os", "$tools", "$report_schema"):          # 占位符都替换干净了
        assert ph not in prompt
    plain = render_system_prompt("plain", tools=["disk_usage"])
    assert "USE 方法" not in plain and len(plain) < len(prompt)


def test_repair_prompt_has_both_placeholders():
    out = repair_prompt("上一条回答")
    assert "上一条回答" in out and '"summary"' in out


def test_json_schema_text_is_valid_json():
    assert json.loads(json_schema_text())["title"] == "DiagnosticReport"


# ---------- 解析 ----------

@pytest.mark.parametrize("raw, ok", [
    (json.dumps(GOOD_REPORT), True),
    ("```json\n" + json.dumps(GOOD_REPORT) + "\n```", True),
    ("这是我的结论：\n" + json.dumps(GOOD_REPORT) + "\n以上。", True),
    ("```\n{\"summary\": \"x\", \"severity\": \"info\", \"confidence\": \"low\"}\n```", True),
    ("完全不是 JSON", False),
    ('{"severity": "info"}', False),                      # 缺 summary / confidence
    ('{"summary": "x", "severity": "bad", "confidence": "low"}', False),  # severity 取值非法
])
def test_parse_report_variants(raw, ok):
    report, error = parse_report(raw)
    assert (report is not None) is ok
    if ok:
        assert isinstance(report, DiagnosticReport) and error is None
    else:
        assert error and report is None


def test_extract_json_handles_nested_braces_and_strings():
    payload = {"summary": "含 } 和 { 的文本", "nested": {"a": 1}}
    data = extract_json("前缀 " + json.dumps(payload, ensure_ascii=False) + " 后缀")
    assert data["summary"] == "含 } 和 { 的文本" and data["nested"]["a"] == 1


def test_report_defaults_and_risk_literal():
    r, _ = parse_report(json.dumps({"summary": "s", "severity": "info", "confidence": "low"}))
    assert r.findings == [] and r.actions == [] and r.root_cause is None
    _, err = parse_report(json.dumps({"summary": "s", "severity": "info", "confidence": "low",
                                      "actions": [{"description": "d", "risk": "whatever"}]}))
    assert "risk" in err


# ---------- Agent 集成 ----------

async def run_agent(script, **kw):
    reg = make_tools()
    llm = MockLLM(script)
    agent = Agent(llm, reg, stream=False, **kw)
    events = [ev async for ev in agent.run("磁盘怎么样")]
    return agent, llm, events


async def test_report_event_parsed_from_plain_json():
    agent, _, events = await run_agent([json.dumps(GOOD_REPORT)])
    report_event = next(e for e in events if e.type == "report")
    assert report_event.data["parsed"] is True
    assert report_event.data["report"]["root_cause"] == "/var/log 未轮转"
    assert events[-1].data["report"]["severity"] == "critical"
    assert agent.last_result.report_error is None


async def test_report_repair_round_when_first_answer_is_prose():
    agent, llm, events = await run_agent(["磁盘看起来满了，建议清理日志", json.dumps(GOOD_REPORT)])
    report_event = next(e for e in events if e.type == "report")
    assert report_event.data["parsed"] is True
    assert len(llm.calls) == 2                                  # 多了一次「改写为 JSON」的请求
    assert "改写成" in llm.calls[1]["messages"][-1].content
    assert events[-1].data["report"]["confidence"] == "medium"


async def test_report_repair_can_be_disabled_and_failure_is_reported():
    _, llm, events = await run_agent(["不是 JSON"], report=False)
    assert not any(e.type == "report" for e in events)
    assert len(llm.calls) == 1

    _, llm2, events2 = await run_agent(["不是 JSON"])
    report_event = next(e for e in events2 if e.type == "report")
    assert report_event.data["parsed"] is False and "找不到合法的 JSON" in report_event.data["error"]
    assert report_event.data["raw"] == "不是 JSON"
    assert events2[-1].data["report_error"]


async def test_tool_calls_still_work_with_report_enabled():
    script = [tool_call("disk_usage", {}), json.dumps(GOOD_REPORT)]
    _, _, events = await run_agent(script)
    assert [e.type for e in events if e.type in ("tool_call", "report")] == ["tool_call", "report"]
    assert events[-1]["data"]["steps"] if False else events[-1].data["steps"] == 2


def test_system_prompt_variant_from_settings(monkeypatch):
    monkeypatch.setenv("SA_PROMPT_VARIANT", "plain")
    from server_agent.config import get_settings
    get_settings.cache_clear()
    try:
        agent = Agent(MockLLM(["x"]), make_tools(), stream=False)
        assert agent.prompt_variant == "plain" and "USE 方法" not in agent.system_prompt
    finally:
        get_settings.cache_clear()
    monkeypatch.setenv("SA_PROMPT_VARIANT", "bogus")
    get_settings.cache_clear()
    with pytest.raises(Exception):
        Settings()
    get_settings.cache_clear()


def test_start_event_carries_variant():
    import asyncio

    agent = Agent(MockLLM(["x"]), make_tools(), stream=False, prompt_variant="plain")

    async def collect():
        return [ev async for ev in agent.run("hi")]

    events = asyncio.run(collect())
    assert events[0].data["prompt_variant"] == "plain"
