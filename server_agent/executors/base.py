"""执行器抽象：工具只说「做什么」，执行器决定「在哪做」。

这是从「管本机」走向「管多机」的关键一步：把「在哪执行」从工具实现里抽出来，
工具就不必关心目标是本地还是远端。沙箱（server_agent/sandbox）是另一条独立通道，只执行 Agent 自己的代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecOutcome:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    host: str = "local"
    elapsed_ms: float = 0.0

    def text(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


class Executor(Protocol):
    """所有执行器都实现这个方法：跑一条命令，拿回结果。"""

    host: str

    async def run(self, argv: list[str], *, timeout: float = 30.0) -> ExecOutcome: ...


class ExecutorError(RuntimeError):
    """连不上、认证失败等「执行前的错误」。"""
