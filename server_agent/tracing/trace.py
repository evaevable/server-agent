"""Trace：一次 run 的耗时树。

为什么需要它？第 05 章的服务暴露了一个事实：一次排查可能要几十秒。当用户问「为什么这么慢」时，
只靠终端日志回答不了——你需要一棵能看「时间花在哪」的树：

    run (12.3s)
    ├─ llm (2.1s)      step=1
    ├─ tool:disk_usage (0.05s)
    ├─ tool:listening_ports (0.02s)
    ├─ llm (8.9s)      step=2
    └─ report (0.01s)

落盘成 JSONL（每行一个 span），既能被 `jq` 查，也能被前端画成瀑布图。
写盘失败不影响主流程（与审计同样的原则）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("server_agent.trace")


@dataclass
class Span:
    name: str
    run_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    start: float = field(default_factory=time.time)
    end: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return round(((self.end or time.time()) - self.start) * 1000, 1)

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "span_id": self.span_id, "name": self.name,
                "start": self.start, "duration_ms": self.duration_ms, "attrs": self.attrs}


class Trace:
    """一个 run 的 span 集合。用法：`async with trace.span("llm", step=1): ...`"""

    def __init__(self, run_id: str, path: str | Path | None = None):
        self.run_id = run_id
        self.spans: list[Span] = []
        self.path = Path(path) / f"{run_id}.jsonl" if path else None

    def start_span(self, name: str, **attrs: Any) -> Span:
        span = Span(name=name, run_id=self.run_id, attrs=attrs)
        self.spans.append(span)
        return span

    def end_span(self, span: Span, **attrs: Any) -> Span:
        span.end = time.time()
        span.attrs.update(attrs)
        self._persist(span)
        return span

    class _Ctx:
        def __init__(self, trace: "Trace", name: str, attrs: dict):
            self.trace, self.name, self.attrs = trace, name, attrs
            self.span: Span | None = None

        async def __aenter__(self) -> Span:
            self.span = self.trace.start_span(self.name, **self.attrs)
            return self.span

        async def __aexit__(self, *exc) -> bool:
            self.trace.end_span(self.span, error=str(exc[1]) if exc[1] else None)
            return False

    def span(self, name: str, **attrs: Any) -> "Trace._Ctx":
        return Trace._Ctx(self, name, attrs)

    def _persist(self, span: Span) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("trace 写入失败: %s", e)

    # ---------- 汇总 ----------
    def summary(self) -> dict:
        total = sum(s.duration_ms for s in self.spans if s.name == "run")
        by_name: dict[str, float] = {}
        for s in self.spans:
            by_name[s.name] = round(by_name.get(s.name, 0) + s.duration_ms, 1)
        llm_ms = sum(v for k, v in by_name.items() if k == "llm")
        tool_ms = sum(v for k, v in by_name.items() if k.startswith("tool:"))
        return {"run_id": self.run_id, "spans": len(self.spans),
                "total_ms": round(total, 1) or round(max((s.duration_ms for s in self.spans), default=0), 1),
                "by_name": by_name, "llm_ms": llm_ms, "tool_ms": tool_ms}

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.spans]


def trace_dir_default() -> str | None:
    from server_agent.config import get_settings

    settings = get_settings()
    return settings.trace_dir if settings.trace_enabled else None
