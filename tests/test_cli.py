import json

from server_agent import __version__
from server_agent.cli import main


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
    assert len(schemas) == 6 and schemas[0]["type"] == "function"


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
