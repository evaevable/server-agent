"""进程级日志配置。

- 业务代码只用 `logging.getLogger(__name__)`，不直接 print（CLI 的用户输出除外）。
- `SA_LOG_FORMAT=json` 时每行一个 JSON 对象，方便 Loki / ELK / CLS 采集；默认是人读的文本格式。
- 通过 `extra={"run_id": ..., "tool": ...}` 传入的字段会原样进入 JSON。
"""

from __future__ import annotations

import json
import logging
import sys
import time

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "info", fmt: str = "text") -> None:
    """幂等：重复调用只会替换 server_agent 根 logger 的 handler。"""
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger("server_agent")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
