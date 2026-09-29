"""沙箱接口：给 Agent 一间隔离的工作室。

沙箱解决的是**另一类**问题：不是「在哪台服务器上执行」，而是
「Agent 自己写的代码，在哪跑才不会伤到人」。

边界（很重要）：
- 沙箱隔离的是 **Agent 生成的代码**，它不能替代目标服务器；
- 不要把生产机的凭证放进沙箱；也不要把沙箱当作操作生产机的通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    backend: str = "unknown"
    sandbox_id: str | None = None
    elapsed_ms: float = 0.0
    artifacts: list[str] = field(default_factory=list)   # 生成的文件（按文件名，内容可由调用方取回）


class Sandbox(Protocol):
    backend: str

    async def run(self, code: str, *, files: dict[str, str] | None = None,
                  timeout: float = 30.0) -> SandboxResult: ...

    async def close(self) -> None: ...


class SandboxError(RuntimeError):
    """沙箱不可用（未安装 Docker、没有 AGS Key、镜像拉取失败等）。"""
