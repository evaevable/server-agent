"""HTTP 接口：提交任务、查询状态、流式接收事件、取消任务。

为什么不是「一个 POST 等到天荒地老」？
Agent 一次排查可能几十秒到几分钟。长连接挂着不仅容易超时，用户也看不到进度。
所以拆成两件事：
1. POST /api/runs          —— 提交任务，立刻拿到 run_id（202 Accepted）
2. GET  /api/runs/{id}/events —— 订阅事件流（SSE），边跑边看

顺带一个好处：手机断网、浏览器刷新之后，带着 Last-Event-ID 重新订阅即可续上。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server_agent.agent.runs import RunManager, make_sse
from server_agent.server.auth import require_token, token_ok

# 大部分接口用标准的 Authorization 头鉴权；唯独 SSE 事件流还要支持 ?token=
# （浏览器 EventSource 无法设置请求头），所以依赖逐个挂在路由上，而不是挂在 router 上。
router = APIRouter(prefix="/api")


class RunCreate(BaseModel):
    input: str = Field(min_length=1, max_length=4000, description="用自然语言描述要排查的问题")


class RunCreated(BaseModel):
    id: str
    status: str
    events_url: str


def manager_of(request: Request) -> RunManager:
    return request.app.state.runs


@router.post("/runs", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(require_token)])
async def create_run(body: RunCreate, request: Request) -> RunCreated:
    """提交一次排查任务。立即返回，不等待 Agent 跑完。"""
    run = await manager_of(request).start(body.input)
    return RunCreated(id=run.id, status=run.status, events_url=f"/api/runs/{run.id}/events")


@router.get("/runs", dependencies=[Depends(require_token)])
def list_runs(request: Request) -> dict:
    runs = manager_of(request).list()
    return {"count": len(runs), "runs": [r.summary() for r in runs]}


@router.get("/runs/{run_id}", dependencies=[Depends(require_token)])
def get_run(run_id: str, request: Request) -> dict:
    run = manager_of(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个 run: {run_id}")
    return run.summary()


@router.delete("/runs/{run_id}", dependencies=[Depends(require_token)])
@router.post("/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_run(run_id: str, request: Request) -> dict:
    mgr = manager_of(request)
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个 run: {run_id}")
    ok = await mgr.cancel(run_id)
    return {"id": run_id, "cancelled": ok, "status": run.status}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request,
                     last_event_id: str | None = None,
                     authorization: str | None = Header(default=None),
                     token: str | None = Query(default=None, description="浏览器 EventSource 无法设请求头，用查询参数传 Token")):
    """SSE 事件流。支持 Last-Event-ID 断线续传（浏览器 EventSource 会自动带上）。

    鉴权接受两种方式：Authorization 头（脚本/服务端调用）或 ?token=（浏览器）。
    Token 出现在 URL 里可能被日志记录，本项目为本地工具，接受这一权衡。
    """
    settings = request.app.state.settings
    provided = token
    if authorization and authorization.lower().startswith("bearer "):
        provided = provided or authorization[7:].strip()
    if not token_ok(settings, provided):
        raise HTTPException(status_code=401, detail="缺少或错误的 API Token",
                            headers={"WWW-Authenticate": "Bearer"})
    mgr = manager_of(request)
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个 run: {run_id}")
    after = 0
    for raw in (last_event_id, request.headers.get("last-event-id")):
        if raw and raw.isdigit():
            after = int(raw)
            break

    async def gen():
        async for ev in mgr.subscribe(run, after_seq=after):
            yield make_sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则事件会被攒着一起发
    })


@router.get("/history", dependencies=[Depends(require_token)])
def history(request: Request, limit: int = 20) -> dict:
    """历史排查记录（来自 SQLite，服务重启后仍在）。"""
    store = request.app.state.store
    if store is None:
        return {"count": 0, "runs": [], "hint": "未启用记忆（SA_MEMORY_ENABLED=false）"}
    runs = store.list_runs(limit=max(1, min(limit, 100)))
    return {"count": len(runs), "runs": runs}


@router.get("/history/{run_id}", dependencies=[Depends(require_token)])
def history_detail(run_id: str, request: Request, after_seq: int = 0) -> dict:
    """某次历史运行的详情与完整事件流（即使在内存里已被淘汰）。"""
    store = request.app.state.store
    run = store.get_run(run_id) if store else None
    if run is None:
        raise HTTPException(status_code=404, detail=f"历史里没有这个 run: {run_id}")
    return {"run": run, "events": store.get_events(run_id, after_seq=after_seq)}


@router.get("/tools", dependencies=[Depends(require_token)])
def list_tools(request: Request) -> dict:
    """暴露**当前 app 实例**实际可用的工具清单（便于前端展示「它能做什么」）。"""
    registry = request.app.state.registry
    return {"count": len(registry), "tools": [
        {"name": t.name, "description": t.description.splitlines()[0], "risk": t.risk,
         "params": list(t.params.model_fields)} for t in registry.list()
    ]}
