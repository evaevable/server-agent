"""Run 管理器：把 Agent 的一次执行变成一个可查询、可订阅、可取消的「任务」。

为什么需要它？
- Agent 一次排查可能跑几十秒。HTTP 请求不能一直挂着等，所以「提交任务」和「接收事件」要分开：
  POST /api/runs 立刻返回 run_id，客户端再单独订阅事件流。
- 事件要在内存里留一份：客户端晚连、断线重连（带 Last-Event-ID）时能补发。
- 任务要能取消：用户点了「停止」，得让 asyncio 任务真的停下来。

存储是内存版（进程重启即丢），第 08 章会换成 SQLite 持久化。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from server_agent.agent.events import AgentResult, Event
from server_agent.agent.loop import Agent

MAX_RUNS = 50  # 内存里最多保留多少个 run（超出后淘汰最旧的已完成 run）
MAX_EVENTS = 2000  # 单个 run 最多保留多少事件（防止长跑任务吃光内存）


@dataclass
class Run:
    id: str
    input: str
    status: str = "running"  # running | done | error | cancelled
    events: list[Event] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: AgentResult | None = None
    task: asyncio.Task | None = None
    error: str | None = None

    def summary(self) -> dict:
        """给列表/详情接口用的轻量视图（不含事件正文）。"""
        return {
            "id": self.id,
            "input": self.input,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "events": len(self.events),
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


class RunManager:
    def __init__(self, agent_factory: Callable[[], Agent]):
        self._factory = agent_factory
        self._runs: dict[str, Run] = {}

    # ---------- 查询 ----------
    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self) -> list[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    # ---------- 创建与执行 ----------
    async def start(self, user_input: str, history: list | None = None) -> Run:
        """创建并启动一个 run。必须是 async：asyncio.create_task 只能在事件循环里调用
        （同步的 FastAPI 路由跑在线程池里，没有事件循环，会报 no running event loop）。"""
        run = Run(id=f"run_{uuid.uuid4().hex[:12]}", input=user_input)
        # 每个 run 一个独立的 Agent 实例：第 04 章说过实例状态（usage / last_result）不适合并发
        agent = self._factory()
        run.task = asyncio.create_task(self._drive(run, agent, history))
        self._runs[run.id] = run
        self._evict()
        return run

    async def _drive(self, run: Run, agent: Agent, history: list | None) -> None:
        try:
            async for ev in agent.run(run.input, history=history):
                run.events.append(ev)
                if len(run.events) > MAX_EVENTS:
                    del run.events[: len(run.events) - MAX_EVENTS]
                if ev.type == "end":
                    run.result = agent.last_result
                for q in list(run.subscribers):
                    q.put_nowait(ev)
            run.status = "cancelled" if run.task and run.task.cancelled() else "done"
        except asyncio.CancelledError:
            run.status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都要让订阅者看到结局
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"
        finally:
            run.finished_at = time.time()
            for q in list(run.subscribers):
                q.put_nowait(None)  # 结束哨兵：让订阅端退出等待

    async def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None or run.status != "running" or run.task is None:
            return False
        run.task.cancel()
        try:
            await run.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return True

    # ---------- 订阅 ----------
    async def subscribe(self, run: Run, after_seq: int = 0) -> AsyncIterator[Event]:
        """先补发 seq > after_seq 的历史事件，再实时推送后续事件，直到 run 结束。

        这是断线续传的关键：重连时带上 Last-Event-ID 作为 after_seq，
        就能从断点继续——不丢事件，也不重复。
        """
        q: asyncio.Queue = asyncio.Queue()
        sent = {e.seq for e in run.events if e.seq <= after_seq}
        live = run.status == "running"
        if live:
            run.subscribers.append(q)
        try:
            while True:
                for ev in list(run.events):  # 缓冲区里还没发过的事件
                    if ev.seq in sent:
                        continue
                    sent.add(ev.seq)
                    yield ev
                if not live:
                    return
                item = await q.get()
                if item is None:
                    live = False  # 任务结束：把缓冲区剩下的发完就退出
                    continue
                if item.seq in sent:  # 已由缓冲区发出，避免重复
                    continue
                sent.add(item.seq)
                yield item
        finally:
            if q in run.subscribers:
                run.subscribers.remove(q)

    # ---------- 淘汰 ----------
    def _evict(self) -> None:
        if len(self._runs) <= MAX_RUNS:
            return
        finished = sorted((r for r in self._runs.values() if r.status != "running"),
                          key=lambda r: r.created_at)
        for r in finished[: len(self._runs) - MAX_RUNS]:
            self._runs.pop(r.id, None)


def make_sse(event: Event) -> str:
    """事件 -> SSE 文本块。id 用 seq，客户端断线重连时可作为 Last-Event-ID。"""
    import json

    return (f"id: {event.seq}\n"
            f"event: {event.type}\n"
            f"data: {json.dumps(event.data, ensure_ascii=False)}\n\n")
