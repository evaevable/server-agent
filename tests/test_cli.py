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
