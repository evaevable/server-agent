import sys
from pathlib import Path

import pytest

# 让 `evals`（评测包）也能被导入：它不在安装的 wheel 里，而是仓库里的顶层包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
