"""运行时健壮性：优雅退出、日志、配置校验、沙箱临时数据清理。"""

import asyncio
import json
import logging
import os

import pytest

from server_agent.agent.loop import Agent
from server_agent.agent.runs import RunManager
from server_agent.config import Settings
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.logging_setup import JsonFormatter, setup_logging
from server_agent.sandbox import DockerSandbox, SandboxError
from server_agent.tools.registry import ToolRegistry


def _slow_manager():
    reg = ToolRegistry()

    @reg.tool(name="slow_probe", timeout=30)
    async def _slow(n: int = 1) -> dict:
        """一个很慢的探测。"""
        await asyncio.sleep(10)
        return {"n": n}

    def factory():
        return Agent(MockLLM([tool_call("slow_probe", {"n": 1}), "完成"]), reg, stream=False)

    return RunManager(factory)


async def test_shutdown_cancels_running_runs():
    mgr = _slow_manager()
    run = await mgr.start("慢查询")
    await asyncio.sleep(0.05)
    assert run.status == "running"
    cancelled = await mgr.shutdown(timeout=2)
    assert cancelled == 1
    assert run.status == "cancelled" and run.finished_at is not None


async def test_shutdown_without_running_runs_is_noop():
    assert await _slow_manager().shutdown() == 0


def test_app_lifespan_shuts_down_run_manager(monkeypatch):
    from fastapi.testclient import TestClient

    from server_agent.server.app import create_app

    app = create_app(Settings(memory_enabled=False, audit_enabled=False, web_dir=""))
    called = []

    async def fake_shutdown(timeout: float = 10.0) -> int:
        called.append(timeout)
        return 0

    monkeypatch.setattr(app.state.runs, "shutdown", fake_shutdown)
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["runs_active"] == 0
    assert called, "应用退出时必须调用 RunManager.shutdown()"


def test_json_log_formatter_keeps_extra_fields():
    record = logging.LogRecord("server_agent.tools", logging.INFO, __file__, 1, "tool ok", (), None)
    record.tool = "disk_usage"
    record.duration_ms = 12.5
    data = json.loads(JsonFormatter().format(record))
    assert data["msg"] == "tool ok" and data["level"] == "info"
    assert data["tool"] == "disk_usage" and data["duration_ms"] == 12.5


def test_setup_logging_is_idempotent():
    setup_logging("debug", "json")
    setup_logging("info", "text")
    root = logging.getLogger("server_agent")
    assert len(root.handlers) == 1 and root.level == logging.INFO and not root.propagate


@pytest.mark.parametrize("field,value", [("log_format", "xml"), ("sandbox_backend", "k8s")])
def test_invalid_settings_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_cli_reports_invalid_config(monkeypatch, capsys):
    from server_agent import cli
    from server_agent.config import get_settings

    monkeypatch.setenv("SA_LOG_FORMAT", "xml")
    get_settings.cache_clear()
    assert cli.main(["version"]) == 2
    assert "配置无效" in capsys.readouterr().err


async def test_docker_sandbox_cleans_input_dir_on_failure(monkeypatch):
    import tempfile

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created.append(path)
        return path

    def no_docker(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr("asyncio.create_subprocess_exec", no_docker)
    with pytest.raises(SandboxError):
        await DockerSandbox().run("print(1)", files={"access.log": "GET / 500\n"})
    assert created and not os.path.exists(created[0]), "启动失败时输入数据目录也必须删除"
