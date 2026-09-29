"""本地执行器：就是 subprocess，但用统一的异步接口包起来。"""

from __future__ import annotations

import asyncio
import time

from server_agent.executors.base import ExecOutcome


class LocalExecutor:
    host = "local"

    async def run(self, argv: list[str], *, timeout: float = 30.0) -> ExecOutcome:
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return ExecOutcome(False, "", f"执行超时（>{timeout}s）", 124, self.host,
                               round((time.perf_counter() - started) * 1000, 1))
        except FileNotFoundError as e:
            return ExecOutcome(False, "", f"命令不存在: {e}", 127, self.host,
                               round((time.perf_counter() - started) * 1000, 1))
        return ExecOutcome(
            ok=(proc.returncode == 0),
            stdout=stdout.decode("utf-8", errors="replace")[:20000],
            stderr=stderr.decode("utf-8", errors="replace")[:8000],
            returncode=proc.returncode or 0, host=self.host,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
