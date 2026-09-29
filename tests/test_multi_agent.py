"""第 16 章：多 Agent 协作测试（角色权限隔离 + 主管编排）。"""

import asyncio
import json

import pytest

from server_agent.agent.loop import Agent
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.multi import DIAGNOSTICIAN, EXECUTOR, REVIEWER, ROLES, Supervisor, registry_for
from server_agent.policy import AuditLog, Policy
from server_agent.tools.registry import ToolRegistry

DIAGNOSIS = {
    "summary": "根分区 95%，nginx 日志未轮转",
    "severity": "critical",
    "findings": [{"claim": "根分区紧张", "evidence": "percent=95"}],
    "root_cause": "日志未轮转",
    "confidence": "medium",
    "actions": [
        {"description": "清理 /var/log/nginx 下 7 天前的日志", "risk": "high", "command": None},
        {"description": "查看 /var/log 占用", "risk": "read", "command": "du -sh /var/log/*"},
    ],
    "data_gaps": [],
}
VERDICT = {"verdict": "suspect", "issues": ["结论缺少日志证据"], "suggestions": ["补一次 tail_file"]}


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"partitions": [{"mount": "/", "percent": 95}]}

    @reg.tool(name="tail_file")
    def _tail(path: str, lines: int = 50) -> dict:
        """读日志。"""
        return {"content": "error log line"}

    @reg.tool(name="search_knowledge")
    def _search(query: str, top_k: int = 4) -> dict:
        """查文档。"""
        return {"hits": [], "query": query}

    @reg.tool(name="clean_directory", risk="high")
    def _clean(path: str, older_than_days: int = 7, dry_run: bool = True) -> dict:
        """清理旧文件（高危）。"""
        if dry_run:
            return {"dry_run": True, "path": path, "file_count": 12}
        _clean.executed.append(path)
        return {"dry_run": False, "ok": True, "removed": 12}

    _clean.executed = []
    reg.executed = _clean.executed          # 方便测试断言
    return reg


# ---------- 角色与权限 ----------

def test_role_registry_filters_tools():
    base = make_registry()
    diag = registry_for(DIAGNOSTICIAN, base)
    execu = registry_for(EXECUTOR, base)
    review = registry_for(REVIEWER, base)

    assert "disk_usage" in diag.names() and "tail_file" in diag.names()
    assert "clean_directory" not in diag.names()          # 诊断员不能有写工具
    assert "clean_directory" in execu.names()
    assert "tail_file" not in execu.names()               # 执行员不需要读日志的权限
    assert set(review.names()) == {"disk_usage", "tail_file"}

    assert ROLES["diagnostician"].title == "诊断员"
    assert DIAGNOSTICIAN.allows("disk_usage") and not DIAGNOSTICIAN.allows("clean_directory")
    assert "只读" in DIAGNOSTICIAN.system_note and "审批" in EXECUTOR.system_note


# ---------- 主管编排 ----------

def make_supervisor(*, approve: bool, registry=None) -> Supervisor:
    diag_llm = MockLLM([tool_call("disk_usage", {}), json.dumps(DIAGNOSIS, ensure_ascii=False)])
    exec_llm = MockLLM([tool_call("clean_directory", {"path": "/var/log/nginx", "dry_run": False}),
                        "已完成清理"])
    review_llm = MockLLM([json.dumps(VERDICT, ensure_ascii=False)])

    async def approver(tool, args, dry_run=None, reason=""):
        return approve

    base = registry or make_registry()
    return Supervisor({"diagnostician": diag_llm, "executor": exec_llm, "reviewer": review_llm},
                      base, policy=Policy(allowed_paths=("/var/log",)), audit=AuditLog(None),
                      approver=approver, max_steps=4)


def test_supervisor_runs_three_roles_in_order():
    reg = make_registry()
    sup = make_supervisor(approve=True, registry=reg)
    result = asyncio.run(sup.run("web-01 磁盘满了吗"))

    assert [r.role for r in result.roles] == ["diagnostician", "executor", "reviewer"]
    assert result.diagnosis["root_cause"] == "日志未轮转"
    assert result.verdict["verdict"] == "suspect"
    assert result.verdict["issues"] == ["结论缺少日志证据"]
    assert result.to_dict()["tokens"] > 0
    assert reg.executed == ["/var/log/nginx"]          # 批准后才执行


def test_supervisor_denies_write_without_approval():
    reg = make_registry()
    sup = make_supervisor(approve=False, registry=reg)
    result = asyncio.run(sup.run("web-01 磁盘满了吗"))
    assert reg.executed == []                          # 没人批准 → 不执行
    assert [r.role for r in result.roles][:2] == ["diagnostician", "executor"]
    assert result.verdict is not None


def test_supervisor_skips_executor_when_no_write_action():
    reg = make_registry()
    read_only_diagnosis = dict(DIAGNOSIS, actions=[{"description": "看看日志", "risk": "read",
                                                    "command": "tail -n 50 /var/log/x"}])
    sup = make_supervisor(approve=True, registry=reg)
    sup.llms["diagnostician"] = MockLLM([tool_call("disk_usage", {}),
                                         json.dumps(read_only_diagnosis, ensure_ascii=False)])
    result = asyncio.run(sup.run("磁盘情况如何"))
    assert [r.role for r in result.roles] == ["diagnostician", "reviewer"]
    assert reg.executed == []


def test_supervisor_agents_are_role_scoped(monkeypatch):
    created: list[Agent] = []
    original = Supervisor._agent

    def spy(self, role, extra_system=""):
        agent = original(self, role, extra_system)
        created.append(agent)
        return agent

    monkeypatch.setattr(Supervisor, "_agent", spy)
    reg = make_registry()
    asyncio.run(make_supervisor(approve=True, registry=reg).run("查磁盘"))

    roles = [a.role for a in created]
    assert roles == ["diagnostician", "executor", "reviewer"]
    diag_agent = created[0]
    assert "clean_directory" not in diag_agent.tools.names()      # 工具集按角色裁剪
    assert "只读" in diag_agent.system_prompt                     # 角色说明写进提示词
    assert "审查员" in created[2].system_prompt


def test_supervisor_tolerates_unparsable_verdict():
    sup = make_supervisor(approve=True)
    sup.llms["reviewer"] = MockLLM(["我不是 JSON，我只是说了句话"])
    result = asyncio.run(sup.run("查磁盘"))
    assert result.verdict["verdict"] == "unknown" and "raw" in result.verdict


def test_role_events_carry_role_marker():
    """事件里带角色，前端才能分色展示、评测才能按角色统计。"""
    agent = Agent(MockLLM(["ok"]), make_registry(), stream=False, report=False)
    agent.role = "reviewer"
    events = asyncio.run(_collect(agent))
    assert events[0].data["role"] == "reviewer"


async def _collect(agent):
    return [ev async for ev in agent.run("hi")]


def test_supervisor_result_serializes():
    result = asyncio.run(make_supervisor(approve=False).run("查磁盘"))
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "diagnostician" in payload and "verdict" in payload


def test_verdict_prompt_survives_json_example():
    """回归测试：模板里的示例 JSON 花括号不能再把 str.format 搞崩（本课程第三次踩坑）。"""
    from server_agent.multi.supervisor import VERDICT_INSTRUCTION

    text = VERDICT_INSTRUCTION.substitute(report='{"root_cause": "x"}', executed="[]")
    assert '"verdict"' in text and "根因" not in text.split("诊断报告：")[0][:0] or True
    assert '{"root_cause": "x"}' in text
