"""Agent 运行时事件。

一次 run 就是一条事件流，按时间顺序产出：
start -> (step -> [reasoning] -> text* -> tool_call* -> tool_result*) ... -> end

事件既是终端输出和前端时间线的数据源（第 06 章），也是第 05 章向外部推送的协议格式。
所有事件都是可 JSON 序列化的（to_dict），字段保持扁平、便于前端渲染。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EventType = Literal["start", "step", "reasoning", "text", "tool_call", "tool_result", "report", "error", "end"]


@dataclass
class Event:
    type: EventType
    run_id: str
    seq: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    """一次 run 的最终产物（end 事件里带的就是它）。"""

    text: str
    steps: int
    tool_calls: int
    usage: Any = None  # server_agent.llm.Usage
    stopped: str = "final"  # final | max_steps | timeout | length | error | cancelled
    messages: list = field(default_factory=list)  # 完整消息历史，便于排查与回放
    report: Any = None  # DiagnosticReport（第 07 章）；解析失败时为 None
    report_error: str | None = None  # 报告解析失败的原因，便于调试提示词

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "steps": self.steps, "tool_calls": self.tool_calls,
                "usage": asdict(self.usage) if self.usage else None, "stopped": self.stopped,
                "report": self.report, "report_error": self.report_error}
