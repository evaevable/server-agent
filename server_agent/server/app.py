"""HTTP 服务入口。第 01 章只有健康检查，第 05 章起在这里挂载 Agent 的 API。"""

from __future__ import annotations

import time

from fastapi import FastAPI

from server_agent import __version__
from server_agent.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂：每次调用返回一个新的 FastAPI 实例，便于测试注入不同配置。"""
    settings = settings or get_settings()
    app = FastAPI(title="server-agent", version=__version__)
    app.state.settings = settings
    app.state.started_at = time.time()

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(time.time() - app.state.started_at, 3),
        }

    return app
