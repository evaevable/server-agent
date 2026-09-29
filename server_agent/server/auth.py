"""鉴权：可选的 Bearer Token。

默认不启用（本地开发方便）；一旦设置了 SA_API_TOKEN，所有 /api 与 /ws 都必须带上。
一个能操作服务器的 Agent 绝不能裸奔在网络上，所以：
- 未设置 Token 时监听非本机地址 → 启动时告警（见 cli.serve）
- Token 校验用常数时间比较，避免时序侧信道
- 配置从 app.state.settings 读，而不是全局单例：测试可以用不同配置建多个 app
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, Request, WebSocket, status

from server_agent.config import Settings


def token_ok(settings: Settings, provided: str | None) -> bool:
    expected = settings.api_token
    if not expected:  # 未启用鉴权
        return True
    return bool(provided) and hmac.compare_digest(provided, expected)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="缺少或错误的 API Token（请设置 Authorization: Bearer <token>）",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    """HTTP 依赖：校验 Authorization: Bearer <token>。"""
    settings: Settings = request.app.state.settings
    if not settings.api_token:
        return
    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not token_ok(settings, provided):
        raise _unauthorized()


def ws_token_ok(ws: WebSocket, token: str | None) -> bool:
    """WebSocket 无法自定义请求头（浏览器端），所以用查询参数 ?token= 传递。"""
    return token_ok(ws.app.state.settings, token)
