"""SSH 执行器：在远端跑命令。

三个工程要点：
1. **连接复用**：一次排查会跑很多条命令，每条约一次握手太慢。这里用一个连接池缓存连接。
2. **host key 校验默认开启**：不做校验等于把中间人风险揽到自己身上；演练环境可在
   inventory 里显式设 `strict_host_key=False`（并明白自己在做什么）。
3. **远端没有 psutil 怎么办**：远端只跑白名单命令，结果由本地的「解析器」变成结构化数据
   （见 tools/remote.py）——这是远程执行的核心取舍：**不为远端装依赖**。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from server_agent.executors.base import ExecOutcome, ExecutorError

try:  # asyncssh 是可选依赖：本地演练不需要它
    import asyncssh
except ImportError:  # pragma: no cover
    asyncssh = None


class SSHExecutor:
    def __init__(self, host: str, *, hostname: str | None = None, port: int = 22,
                 username: str | None = None, password: str | None = None,
                 key_path: str | None = None, strict_host_key: bool = True,
                 connect_timeout: float = 10.0):
        self.host = host
        self.hostname = hostname or host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.strict_host_key = strict_host_key
        self.connect_timeout = connect_timeout
        self._conn: Any = None
        self._lock = asyncio.Lock()

    # ---------- 连接管理 ----------
    async def _connect(self):
        if asyncssh is None:  # pragma: no cover
            raise ExecutorError("未安装 asyncssh：pip install asyncssh")
        opts: dict[str, Any] = {"port": self.port, "known_hosts": None if not self.strict_host_key else (),
                                "connect_timeout": self.connect_timeout}
        if self.username:
            opts["username"] = self.username
        if self.password:
            opts["password"] = self.password
        if self.key_path:
            opts["client_keys"] = [self.key_path]
        try:
            return await asyncssh.connect(self.hostname, **opts)
        except Exception as e:  # noqa: BLE001 —— 各种连接异常统一成 ExecutorError
            raise ExecutorError(f"连接 {self.host}（{self.hostname}:{self.port}）失败: {e}") from e

    async def _get_conn(self):
        async with self._lock:
            if self._conn is None:
                self._conn = await self._connect()
            return self._conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                try:
                    await self._conn.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None

    # ---------- 执行 ----------
    async def run(self, argv: list[str], *, timeout: float = 30.0) -> ExecOutcome:
        started = time.perf_counter()
        cmd = " ".join(_quote(a) for a in argv)
        try:
            conn = await self._get_conn()
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        except asyncio.TimeoutError:
            return ExecOutcome(False, "", f"远端执行超时（>{timeout}s）", 124, self.host,
                               round((time.perf_counter() - started) * 1000, 1))
        except ExecutorError:
            raise
        except Exception as e:  # noqa: BLE001 —— 连接断了就丢弃，下次重连
            self._conn = None
            return ExecOutcome(False, "", f"远端执行失败: {e}", 255, self.host,
                               round((time.perf_counter() - started) * 1000, 1))
        return ExecOutcome(
            ok=(result.exit_status == 0),
            stdout=(result.stdout or "")[:20000],
            stderr=(result.stderr or "")[:8000],
            returncode=result.exit_status or 0, host=self.host,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _quote(arg: str) -> str:
    """最小化 shell 转义：远端通过 shell 执行，参数必须安全地引起来。"""
    if arg == "":
        return "''"
    if all(ch.isalnum() or ch in "-_./=:@," for ch in arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"
