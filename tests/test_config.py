import pytest
from pydantic import ValidationError

from server_agent.config import Settings


def test_defaults_listen_local_only():
    s = Settings()
    assert s.host == "127.0.0.1" and s.port == 8000 and s.api_token is None


def test_env_file_is_loaded(tmp_path):
    (tmp_path / ".env").write_text("SA_PORT=9000\nSA_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    s = Settings()
    assert s.port == 9000 and s.log_level == "debug"


def test_env_var_overrides_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SA_PORT=9000\n", encoding="utf-8")
    monkeypatch.setenv("SA_PORT", "9100")
    assert Settings().port == 9100


def test_invalid_values_rejected(monkeypatch):
    monkeypatch.setenv("SA_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_token_masked_and_empty_is_none(monkeypatch):
    monkeypatch.setenv("SA_API_TOKEN", "secret")
    assert Settings().public_dict()["api_token"] == "***"
    monkeypatch.setenv("SA_API_TOKEN", "")
    assert Settings().api_token is None
