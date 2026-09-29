"""HTTP 服务入口。

第 01 章只有健康检查；第 05 章挂上了 Agent 的 API 与 WebSocket。
第 06 章会在这里托管前端静态文件。

应用工厂模式：create_app(settings, agent_factory) 每次返回一个新的 FastAPI 实例，
测试可以注入 MockLLM 的 agent_factory，完全离线运行。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server_agent import __version__
from server_agent.agent.loop import Agent
from server_agent.agent.runs import RunManager
from server_agent.config import Settings, get_settings
from server_agent.server import api, ws


def default_agent_factory() -> Agent:
    """生产路径：按配置创建真实模型客户端 + 默认工具注册表。"""
    from server_agent.llm import create_llm

    return Agent(create_llm())


def create_app(settings: Settings | None = None,
               agent_factory: Callable[[], Agent] | None = None,
               tools_registry=None,
               store=None) -> FastAPI:
    settings = settings or get_settings()
    if tools_registry is None:
        from server_agent.tools import registry as tools_registry  # noqa: PLC0415
    if store is None and settings.memory_enabled:
        from server_agent.memory import get_store  # noqa: PLC0415

        store = get_store()
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.runs.shutdown()          # 优雅退出：取消在跑的 run，撤销待审批

    app = FastAPI(title="server-agent", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.started_at = time.time()
    app.state.agent_factory = agent_factory or default_agent_factory
    app.state.registry = tools_registry
    app.state.store = store
    # 第 09 章：策略、审批、审计
    from server_agent.policy import ApprovalManager, Policy, get_audit  # noqa: PLC0415

    app.state.policy = Policy(allowed_paths=tuple(settings.policy_allow_paths),
                              allowed_services=tuple(settings.policy_allow_services),
                              require_approval=settings.policy_require_approval)
    app.state.approvals = ApprovalManager(timeout=settings.approval_timeout)
    app.state.audit = get_audit() if settings.audit_enabled else None
    app.state.runs = RunManager(app.state.agent_factory, store=store,
                                approvals=app.state.approvals, policy=app.state.policy,
                                audit=app.state.audit)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(time.time() - app.state.started_at, 3),
            "auth": bool(settings.api_token),
            "runs_active": sum(1 for r in app.state.runs.list() if r.status == "running"),
        }

    app.include_router(api.router)
    app.include_router(ws.router)

    # 前端静态文件（第 06 章）。放在最后注册：/api、/ws 先匹配，不会被静态资源吞掉。
    web_dir = settings.web_dir
    if web_dir and Path(web_dir).is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
