"""审计日志：谁、什么时候、对什么、做了什么、结果如何。

写成 JSONL（每行一个 JSON）而不是 SQLite，理由：
- 追加写、不怕并发、不会被业务库的迁移影响；
- 用 `tail -f`、`jq` 就能查，运维同学最熟悉的形态；
- 关键安全记录不应该和「可被清空的业务数据」混在一起。

同时提供一个内存版 `records()` 供测试与前端展示。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from server_agent.policy.redact import redact_args

log = logging.getLogger("server_agent.audit")


@dataclass
class AuditRecord:
    ts: float
    event: str            # tool_call | tool_result | approval | denied | error
    run_id: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    decision: str = ""    # allow | approval | forbidden
    approved: bool | None = None
    detail: str = ""
    duration_ms: float | None = None
    ok: bool | None = None

    def line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class AuditLog:
    def __init__(self, path: str | Path | None = "data/audit.jsonl", *, keep_in_memory: int = 500):
        self.path = str(path) if path else None
        self.keep = keep_in_memory
        self._records: list[AuditRecord] = []
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: AuditRecord) -> AuditRecord:
        record.args = redact_args(record.args or {})     # 落盘前脱敏
        record.detail = record.detail[:1000]
        self._records.append(record)
        if len(self._records) > self.keep:
            self._records = self._records[-self.keep:]
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(record.line() + "\n")
            except OSError as e:      # 审计写失败不能拖垮主流程，但必须吵
                log.error("审计日志写入失败: %s", e)
        return record

    def log(self, event: str, **kwargs: Any) -> AuditRecord:
        return self.write(AuditRecord(ts=time.time(), event=event, **kwargs))

    def records(self, limit: int = 100) -> list[dict]:
        return [asdict(r) for r in self._records[-limit:]]

    def read_file(self, limit: int = 200) -> list[dict]:
        if not self.path or not Path(self.path).exists():
            return []
        lines = Path(self.path).read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


_default: AuditLog | None = None


def get_audit() -> AuditLog:
    global _default
    if _default is None:
        from server_agent.config import get_settings

        s = get_settings()
        _default = AuditLog(s.audit_path if s.audit_enabled else None)
    return _default


def reset_audit(log_: AuditLog | None = None) -> None:
    global _default
    _default = log_
