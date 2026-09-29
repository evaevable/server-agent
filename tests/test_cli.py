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
    assert set(types[2:-1]) == {"text"}
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
