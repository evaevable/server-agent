"""人工审批：高危操作必须停下来等人点头。

状态机：
    pending --approve--> approved
            --deny-----> denied
            --超时------> timeout（等同拒绝）

两个关键设计：
1. **默认拒绝**：超时、连接断开、进程重启都算拒绝。「没人批准」绝不能等价于「批准了」。
2. **审批前先给 dry-run 结果**：让审批人看到「将要做什么、影响到什么」再决定，
   而不是只看到一个工具名。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Approval:
    id: str
    run_id: str
    tool: str
    args: dict[str, Any]
    risk: str = "high"
    dry_run: Any = None            # 预演结果：审批人据此判断
    reason: str = ""
    status: str = "pending"        # pending | approved | denied | timeout
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    decider: str | None = None
    note: str | None = None
    _future: asyncio.Future | None = None

    def public(self) -> dict:
        return {"id": self.id, "run_id": self.run_id, "tool": self.tool, "args": self.args,
                "risk": self.risk, "dry_run": self.dry_run, "reason": self.reason,
                "status": self.status, "created_at": self.created_at,
                "decided_at": self.decided_at, "decider": self.decider, "note": self.note}


class ApprovalManager:
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._pending: dict[str, Approval] = {}
        self._history: list[Approval] = []

    # ---------- 查询 ----------
    def get(self, approval_id: str) -> Approval | None:
        return self._pending.get(approval_id) or next(
            (a for a in self._history if a.id == approval_id), None)

    def pending(self, run_id: str | None = None) -> list[Approval]:
        return [a for a in self._pending.values() if run_id is None or a.run_id == run_id]

    def history(self, limit: int = 50) -> list[Approval]:
        return (self._history + list(self._pending.values()))[-limit:]

    # ---------- 发起与裁决 ----------
    async def request(self, run_id: str, tool: str, args: dict, *, risk: str = "high",
                      dry_run: Any = None, reason: str = "") -> Approval:
        """发起审批并等待结果。超时/无人批准一律视为拒绝。"""
        approval = Approval(id=f"ap_{uuid.uuid4().hex[:10]}", run_id=run_id, tool=tool, args=args,
                            risk=risk, dry_run=dry_run, reason=reason)
        approval._future = asyncio.get_event_loop().create_future()
        self._pending[approval.id] = approval
        return approval

    async def wait(self, approval: Approval) -> Approval:
        try:
            await asyncio.wait_for(approval._future, timeout=self.timeout)
        except asyncio.TimeoutError:
            approval.status = "timeout"
            approval.note = f"超过 {self.timeout:.0f} 秒无人审批，按拒绝处理"
        approval.decided_at = approval.decided_at or time.time()
        self._pending.pop(approval.id, None)
        self._history.append(approval)
        return approval

    def resolve(self, approval_id: str, approved: bool, *, decider: str = "user",
                note: str | None = None) -> Approval | None:
        approval = self._pending.get(approval_id)
        if approval is None:
            return None
        approval.status = "approved" if approved else "denied"
        approval.decided_at = time.time()
        approval.decider = decider
        approval.note = note
        if approval._future and not approval._future.done():
            approval._future.set_result(approved)
        return approval

    def cancel_run(self, run_id: str, reason: str = "run 已结束") -> None:
        """run 结束时，未决审批全部按拒绝处理（不能让它们悬着）。"""
        for approval in self.pending(run_id):
            self.resolve(approval.id, False, decider="system", note=reason)

    # ---------- 给 Agent 用的 approver ----------
    def make_approver(self, run_id: str, on_request=None):
        """返回一个 async 回调：Agent 需要审批时调用它。

        `on_request(approval)` 用于把审批请求推给客户端（SSE/WS），
        然后在这里等 `resolve()` 被调用。
        """
        async def approver(tool: str, args: dict, dry_run: Any = None, reason: str = "") -> bool:
            approval = await self.request(run_id, tool, args, dry_run=dry_run, reason=reason)
            if on_request:
                result = on_request(approval)
                if asyncio.iscoroutine(result):
                    await result
            done = await self.wait(approval)
            return done.status == "approved"

        return approver
