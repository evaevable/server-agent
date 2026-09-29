import pytest

from server_agent.config import get_settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """每个测试都在空目录里跑，且清掉 SA_ 环境变量，避免本机 .env 干扰。"""
    monkeypatch.chdir(tmp_path)
    import os

    for k in list(os.environ):
        if k.startswith(("SA_", "LLM_")):
            monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
