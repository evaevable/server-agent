import pytest

from server_agent.cli import main
from server_agent.config import Settings
from server_agent.llm import LLMError, create_llm
from server_agent.llm.mock import MockLLM
from server_agent.llm.openai_compat import OpenAICompatClient


def test_factory_mock_and_missing_config(monkeypatch):
    assert isinstance(create_llm(Settings(), mock=True), MockLLM)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    assert isinstance(create_llm(Settings()), MockLLM)
    monkeypatch.delenv("LLM_PROVIDER")
    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        create_llm(Settings())


def test_factory_real_client_and_alias(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("SA_LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "sk-1")
    s = Settings()
    assert isinstance(create_llm(s), OpenAICompatClient)
    assert s.public_dict()["llm_api_key"] == "***"


def test_cli_chat_once_mock(capsys):
    assert main(["chat", "--mock", "--once", "df 是什么"]) == 0
    assert "（mock）你说的是：df 是什么" in capsys.readouterr().out


def test_cli_chat_without_config_exits_2(capsys):
    assert main(["chat", "--once", "hi"]) == 2
    assert "缺少模型配置" in capsys.readouterr().err


def test_extra_body_from_env_json(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}')
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL", "m")
    c = create_llm(Settings())
    assert c.extra_body == {"thinking": {"type": "disabled"}}
