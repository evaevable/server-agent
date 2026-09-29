"""WebSocket 通道：双向交互。

与 SSE 的分工：
- SSE 单向（服务端 -> 客户端），够用来看进度，浏览器 EventSource 会自动重连；
- WebSocket 双向，用于「服务端要问客户端」的场景——第 09 章的人工审批就是这样：
  Agent 想执行高危操作，必须停下来问「批准吗」，客户端回「批准/拒绝」，Agent 才能继续。

协议（客户端 -> 服务端 JSON）：
    {"type": "ask",       "input": "磁盘为什么满了"}
    {"type": "cancel",    "run_id": "run_xxx"}
    {"type": "approval",  "approval_id": "ap_xxx", "approved": true}
    {"type": "ping"}
服务端 -> 客户端：与 SSE 相同的事件对象，外加 {"type": "ready"} 与 {"type": "pong"}。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server_agent.agent.runs import RunManager
from server_agent.server.auth import ws_token_ok

router = APIRouter()


class WSSession:
    """一个连接上可能有多个 run 同时在跑，需要各自转发事件。"""

    def __init__(self, ws: WebSocket, mgr: RunManager):
        self.ws = ws
        self.mgr = mgr
        self.tasks: dict[str, asyncio.Task] = {}
        self.send_lock = asyncio.Lock()

    async def send(self, payload: dict) -> None:
        async with self.send_lock:  # 多个 run 并发写同一个连接，必须串行化
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def forward(self, run_id: str) -> None:
        run = self.mgr.get(run_id)
        if run is None:
            return
        async for ev in self.mgr.subscribe(run):
            await self.send(ev.to_dict())

    async def close(self) -> None:
        for t in self.tasks.values():
            t.cancel()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = None) -> None:
    if not ws_token_ok(ws, token):
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    session = WSSession(ws, ws.app.state.runs)
    await session.send({"type": "ready", "tools": ws.app.state.registry.names()})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await session.send({"type": "error", "message": "消息不是合法 JSON"})
                continue
            kind = msg.get("type")
            if kind == "ping":
                await session.send({"type": "pong"})
            elif kind == "ask":
                user_input = (msg.get("input") or "").strip()
                if not user_input:
                    await session.send({"type": "error", "message": "input 不能为空"})
                    continue
                run = await session.mgr.start(user_input)
                await session.send({"type": "accepted", "run_id": run.id})
                session.tasks[run.id] = asyncio.create_task(session.forward(run.id))
            elif kind == "approval":
                approval_id = msg.get("approval_id") or ""
                approval = ws.app.state.approvals.get(approval_id)
                if approval is None:
                    await session.send({"type": "error", "message": f"没有这次审批: {approval_id}"})
                    continue
                done = ws.app.state.approvals.resolve(approval_id, bool(msg.get("approved")),
                                                     decider="ws", note=msg.get("note"))
                await session.send({"type": "approval_resolved", "approval": done.public()})
            elif kind == "cancel":
                run_id = msg.get("run_id") or ""
                ok = await session.mgr.cancel(run_id)
                await session.send({"type": "cancelled", "run_id": run_id, "ok": ok})
            else:
                await session.send({"type": "error", "message": f"未知消息类型: {kind}"})
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
