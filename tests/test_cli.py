import json

from server_agent import __version__
from server_agent.cli import main
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.tools import registry


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"server-agent {__version__}"


def test_config_masks_token(capsys, monkeypatch):
    monkeypatch.setenv("SA_API_TOKEN", "secret")
    assert main(["config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["api_token"] == "***" and "secret" not in json.dumps(out)


def test_tools_list_and_schema(capsys):
    assert main(["tools", "list"]) == 0
    out = capsys.readouterr().out
    for name in ("host_info", "disk_usage", "tail_file"):
        assert name in out
    assert main(["tools", "list", "--schema"]) == 0
    schemas = json.loads(capsys.readouterr().out)
    names = {s["function"]["name"] for s in schemas}
    assert len(schemas) >= 18 and schemas[0]["type"] == "function"
    assert {"disk_usage", "recall_host", "remember_fact"} <= names


def test_tools_call_ok_and_error(capsys):
    assert main(["tools", "call", "disk_usage", '{"path": "/"}']) == 0
    assert "partitions" in capsys.readouterr().out
    assert main(["tools", "call", "disk_usage", '{"oops": 1}']) == 1
    assert "Extra inputs" in capsys.readouterr().out


def test_ask_mock_json_events(capsys):
    assert main(["ask", "--mock", "--json", "磁盘为什么满"]) == 0
    events = json.loads(capsys.readouterr().out)
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[1] == "step" and types[-1] == "end"
    assert set(types[2:-1]) <= {"text", "report"}      # 中间只有流式文本与报告事件
    assert "report" in types
    assert events[-1]["seq"] == len(events)  # seq 连续
    assert events[0]["run_id"].startswith("run_") and events[0]["seq"] == 1
    assert events[-1]["data"]["stopped"] == "final" and "磁盘为什么满" in events[-1]["data"]["text"]


def test_ask_mock_human_readable(capsys):
    assert main(["ask", "--mock", "机器卡吗"]) == 0
    captured = capsys.readouterr()
    assert "（mock）你说的是：机器卡吗" in captured.out
    assert "1 步" in captured.err and "[final]" in captured.err
    assert main(["ask", "--mock", "-q", "机器卡吗"]) == 0
    assert "── 第" not in capsys.readouterr().err


def test_ask_without_config_exits_2(capsys):
    assert main(["ask", "hi"]) == 2
    assert "缺少模型配置" in capsys.readouterr().err


def test_ask_suppresses_raw_json_stream(capsys):
    """结构化报告是给机器看的：终端不应把整段 JSON 逐字刷出来。"""
    import json as _json

    from server_agent.agent.loop import Agent
    from server_agent.config import Settings
    from server_agent.server.app import create_app
    from server_agent.llm.mock import MockLLM

    report = {"summary": "磁盘 97%", "severity": "critical", "confidence": "high",
              "findings": [{"claim": "c", "evidence": "e"}], "actions": [], "data_gaps": []}

    class OneShot:
        """用 mock 的脚本回放机制，但只跑一次 ask。"""

        def __init__(self):
            self.llm = MockLLM([_json.dumps(report, ensure_ascii=False)])

    # 直接走 CLI 的渲染函数，避免创建真实服务
    from server_agent.agent.events import Event
    from server_agent.cli import _render

    _render(Event("step", "r", 1, {"step": 1}), verbose=False, quiet=False)
    _render(Event("text", "r", 2, {"text": '{"summary": "磁盘 97%", '}), verbose=False, quiet=False)
    _render(Event("text", "r", 3, {"text": '"severity": "critical"}'}), verbose=False, quiet=False)
    _render(Event("report", "r", 4, {"parsed": True, "report": report, "error": None}), verbose=False, quiet=False)
    err = capsys.readouterr().err
    assert "正在生成结构化报告" in err
    assert '"severity"' not in err          # 原始 JSON 没有被刷到终端
    assert "critical" in err                # 报告摘要仍然展示


def test_report_text_renders_readable_summary(capsys):
    from server_agent.agent.events import Event
    from server_agent.cli import _render

    report = {"summary": "根分区 97%", "severity": "critical", "confidence": "medium",
              "root_cause": "日志未轮转",
              "findings": [{"claim": "分区将满", "evidence": "percent=97"}],
              "actions": [{"description": "查看大文件", "risk": "read", "command": "du -sh /var/log/*"}],
              "data_gaps": ["未确认 logrotate"]}
    _render(Event("end", "r", 9, {"text": "{...json...}", "report": report, "stopped": "final",
                                  "steps": 2, "tool_calls": 1, "elapsed_ms": 12.0, "usage": {}}),
            verbose=False, quiet=False)
    out = capsys.readouterr().out
    assert "[critical] 根分区 97%" in out and "根因：日志未轮转" in out
    assert "命令：du -sh /var/log/*" in out and "{...json...}" not in out


def test_ask_injects_memory_and_quiet_mode(capsys, monkeypatch, tmp_path):
    """有历史记录时，ask 会把「历史记忆」注入系统提示词并在 stderr 提示。"""
    from server_agent.memory import Store, reset_store

    db = tmp_path / "mem.db"
    store = Store(db)
    store.save_run_start("run_old", "web-01 磁盘快满了")
    store.save_run_end("run_old", status="done", steps=2, tool_calls=1, tokens=100,
                       report={"summary": "根分区 92%", "root_cause": "日志未轮转", "confidence": "high"})
    store.close()

    monkeypatch.setenv("SA_DB_PATH", str(db))
    reset_store()
    try:
        assert main(["ask", "--mock", "--host", "web-01", "再看一眼磁盘"]) == 0
        err = capsys.readouterr().err
        assert "[记忆] 已注入历史记忆" in err

        assert main(["ask", "--mock", "--host", "web-01", "-q", "再来一次"]) == 0
        assert "[记忆]" not in capsys.readouterr().err

        assert main(["ask", "--mock", "--host", "web-01", "--no-memory", "再来一次"]) == 0
        assert "[记忆]" not in capsys.readouterr().err
    finally:
        reset_store()


def test_history_command_lists_and_shows(capsys, monkeypatch, tmp_path):
    from server_agent.memory import Store, reset_store

    db = tmp_path / "hist.db"
    store = Store(db)
    store.save_run_start("run_h", "端口被谁占了")
    store.append_event("run_h", 1, "start", {"input": "端口被谁占了"})
    store.save_run_end("run_h", status="done", steps=1, tool_calls=1, tokens=42, text="结论",
                       report={"summary": "80 端口被 nginx 占用", "confidence": "high"})
    store.close()
    monkeypatch.setenv("SA_DB_PATH", str(db))
    reset_store()
    try:
        assert main(["history"]) == 0
        out = capsys.readouterr().out
        assert "run_h" in out and "80 端口被 nginx 占用" in out

        assert main(["history", "--show", "run_h", "--events"]) == 0
        out = capsys.readouterr().out
        assert '"status": "done"' in out and "start:" in out

        assert main(["history", "--show", "run_missing"]) == 1
        assert "没有这条记录" in capsys.readouterr().err
    finally:
        reset_store()


def test_history_empty(capsys, monkeypatch, tmp_path):
    from server_agent.memory import reset_store

    monkeypatch.setenv("SA_DB_PATH", str(tmp_path / "empty.db"))
    reset_store()
    try:
        assert main(["history"]) == 0
        assert "还没有历史记录" in capsys.readouterr().out
    finally:
        reset_store()


def test_ask_with_no_approval_denies_and_audits(capsys, monkeypatch, tmp_path):
    """--no-approval：高危操作直接拒绝；拒绝会写进审计日志。"""
    from server_agent.agent.loop import Agent
    from server_agent.policy import AuditLog, Policy, reset_audit
    from server_agent.tools import ops

    monkeypatch.setattr(ops, "_service_restart", lambda name: (_ for _ in ()).throw(AssertionError("不应执行")))
    audit = AuditLog(tmp_path / "audit.jsonl")
    reset_audit(audit)
    monkeypatch.setenv("SA_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    async def run():
        llm = MockLLM([tool_call("restart_service", {"name": "nginx", "dry_run": False}), "好的"])
        agent = Agent(llm, registry, stream=False, policy=Policy(), audit=audit, report=False)
        return [ev async for ev in agent.run("重启 nginx")]

    import asyncio

    events = asyncio.run(run())
    tr = next(e for e in events if e.type == "tool_result")
    assert tr.data["ok"] is False and "审批" in tr.data["content"]
    decisions = [r["decision"] for r in audit.records() if r["event"] in ("approval", "denied")]
    assert "approval" in decisions


def test_audit_command(capsys, monkeypatch, tmp_path):
    from server_agent.policy import AuditLog, reset_audit

    audit = AuditLog(tmp_path / "a.jsonl")
    audit.log("tool_call", run_id="r1", tool="disk_usage", decision="allow")
    audit.log("denied", run_id="r1", tool="clean_directory", decision="forbidden", detail="路径不在白名单")
    reset_audit(audit)
    try:
        assert main(["audit"]) == 0
        out = capsys.readouterr().out
        assert "disk_usage" in out and "forbidden" in out
        assert main(["audit", "-v"]) == 0
        assert "路径不在白名单" in capsys.readouterr().out
    finally:
        reset_audit()


def test_audit_command_empty(capsys, monkeypatch, tmp_path):
    from server_agent.policy import AuditLog, reset_audit

    reset_audit(AuditLog(tmp_path / "none.jsonl"))
    try:
        assert main(["audit"]) == 0
        assert "还没有审计记录" in capsys.readouterr().out
    finally:
        reset_audit()


def test_tools_call_goes_through_policy(capsys, monkeypatch, tmp_path):
    """回归测试：`tools call` 也不能绕过策略层。

    曾经出现过的真实漏洞：这条路径曾经直接执行了 `rm -rf /`。
    安全的正确姿势是「挂到所有入口」，而不是只挂 Agent 循环。
    """
    from server_agent.policy import AuditLog, reset_audit

    audit = AuditLog(tmp_path / "t.jsonl")
    reset_audit(audit)
    monkeypatch.setenv("SA_AUDIT_PATH", str(tmp_path / "t.jsonl"))
    try:
        # 危险命令：策略直接拒绝，且不执行
        assert main(["tools", "call", "run_command", '{"command": "rm -rf /"}']) == 1
        out = capsys.readouterr()
        assert "策略拒绝" in out.out
        assert any(r["decision"] == "forbidden" for r in audit.records())

        # 高危操作：非交互模式下按拒绝处理
        assert main(["tools", "call", "restart_service", '{"name": "nginx"}', "--no-approval"]) == 1
        assert "审批未通过" in capsys.readouterr().out

        # 只读工具正常放行
        assert main(["tools", "call", "host_info"]) == 0
        assert "hostname" in capsys.readouterr().out
    finally:
        reset_audit()


def test_ask_multi_mock_runs(capsys):
    """--multi 至少要能跑通（用 echo MockLLM：三段角色输出 + 裁决 unknown）。"""
    assert main(["ask", "--multi", "--mock", "查一下磁盘"]) == 0
    out = capsys.readouterr().out
    assert "=== diagnostician" in out and "=== reviewer" in out
    assert "[审查]" in out


def test_ask_multi_json_output(capsys):
    assert main(["ask", "--multi", "--mock", "--json", "查一下"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roles"] and payload["verdict"]["verdict"] in ("unknown", "ok", "suspect")
    assert payload["tokens"] >= 0


def test_eval_command(capsys):
    assert main(["eval", "--cases", "evals/cases"]) == 0
    out = capsys.readouterr().out
    assert "用例数" in out and "通过率" in out


def test_mcp_list_command(capsys):
    import sys as _sys

    assert main(["mcp", "list", "--server", f"{_sys.executable} -m server_agent.mcp.server"]) == 0
    out = capsys.readouterr().out
    assert "host_info" in out and "restart_service" not in out
