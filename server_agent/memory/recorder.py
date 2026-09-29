"""把一次 run 的过程写进 Store。

CLI 与 HTTP 服务都走这里，保证「终端跑的」和「页面上跑的」都能在历史里查到。
写库失败不能影响 Agent 本身（磁盘满、权限问题都可能发生），所以这里所有异常都吞掉并记日志。
"""

from __future__ import annotations

import logging

from server_agent.agent.events import AgentResult, Event
from server_agent.memory.store import Store

log = logging.getLogger("server_agent.memory")


class Recorder:
    def __init__(self, store: Store, run_id: str = "", user_input: str = "", *,
                 pending_run_id: str | None = None):
        """run_id 可以稍后绑定：CLI 拿不到 Agent 内部生成的 id，所以先建对象、收到 start 事件再写。

        （参数 pending_run_id 仅作可读性占位，实际等价于传 run_id=""）
        """
        self.store = store
        self.run_id = pending_run_id if run_id == "" and pending_run_id is not None else run_id
        self.input = user_input
        self.persisted = bool(self.run_id)
        if self.persisted:
            try:
                store.save_run_start(self.run_id, user_input)
            except Exception as e:  # noqa: BLE001
                log.warning("写入 run 起始记录失败：%s", e)
                self.persisted = False

    def bind(self, run_id: str) -> None:
        """绑定真实 run_id 并写入起始记录。"""
        self.run_id = run_id
        try:
            self.store.save_run_start(run_id, self.input)
            self.persisted = True
        except Exception as e:  # noqa: BLE001
            log.warning("写入 run 起始记录失败：%s", e)

    def event(self, ev: Event) -> None:
        if not self.persisted or ev.type == "end":
            return
        try:
            self.store.append_event(self.run_id, ev.seq, ev.type, ev.data)
        except Exception as e:  # noqa: BLE001
            log.warning("写入事件失败：%s", e)

    def finish(self, result: AgentResult, status: str = "done", error: str | None = None) -> None:
        if not self.persisted:
            return
        try:
            usage = result.usage
            self.store.save_run_end(
                self.run_id, status=status, steps=result.steps, tool_calls=result.tool_calls,
                tokens=(usage.total_tokens if usage else 0), text=result.text,
                report=result.report, error=error)
        except Exception as e:  # noqa: BLE001
            log.warning("写入 run 结束记录失败：%s", e)

    @classmethod
    def from_result(cls, store: Store, run_id: str, user_input: str, result: AgentResult,
                    status: str = "done", error: str | None = None) -> "Recorder":
        r = cls(store, run_id, user_input)
        r.finish(result, status=status, error=error)
        return r


def build_memory_context(store: Store, host: str | None = None, limit: int = 3) -> str | None:
    """给新一轮排查准备「历史记忆」文本：最近几次结论 + 该主机的事实。

    这是长期记忆的读路径：新会话开始时把它作为 context 塞进对话，
    Agent 就不必从零开始（「上次也是这个原因」）。
    """
    incidents = store.recent_incidents(host=host, limit=limit)
    facts = store.recall_facts(host, limit=8) if host else []
    if not incidents and not facts:
        return None
    lines = ["[历史记忆] 以下是之前排查的结论，可作为参考，但仍需用工具核实，不要直接照搬："]
    for it in incidents:
        lines.append(f"- {it['input']}：{it['summary']}（根因：{it['root_cause']}；置信度 {it['confidence']}）")
    if facts:
        lines.append(f"已知事实（{host}）：")
        lines += [f"- {f['key']} = {f['value']}" for f in facts]
    return "\n".join(lines)
