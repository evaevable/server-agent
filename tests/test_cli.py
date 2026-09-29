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
