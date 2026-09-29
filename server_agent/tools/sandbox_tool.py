"""`run_python`：让 Agent 在沙箱里执行自己写的分析代码（CodeAct）。

为什么需要它？
固定工具能回答的问题是**预先想好的**。而「统计 nginx 日志里 5xx 的分钟分布」这类问题，
与其为它写一个专用工具（还会有第二十个、第三十个），不如让模型**写几行代码**——
灵活、可组合，代价是「代码不可信」，所以必须放进沙箱。

边界（见 docs/adr/0003-sandbox-boundary.md）：
- 沙箱隔离的是 **Agent 自己执行的代码**，不是目标服务器；
- 沙箱里不放任何服务器凭证；
- 只能通过 `files` 参数把「已经取回来的数据」送进去，不能让它去连服务器。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from server_agent.config import get_settings
from server_agent.sandbox import SandboxError, create_sandbox
from server_agent.tools.registry import ToolError, tool

MAX_CODE_CHARS = 8000


def _make_sandbox():
    settings = get_settings()
    backend = settings.sandbox_backend
    if backend == "local_docker":
        from server_agent.sandbox.local_docker import DockerSandbox

        return DockerSandbox(image=settings.sandbox_image)
    if backend == "ags":
        from server_agent.sandbox.ags import AGSSandbox

        return AGSSandbox()
    return create_sandbox()


@tool(risk="low", max_chars=6000)
async def run_python(
    code: Annotated[str, Field(min_length=1, max_length=MAX_CODE_CHARS,
                               description="要执行的 Python 代码。只能读 /data 下的输入文件，不能联网")],
    purpose: Annotated[str, Field(max_length=200, description="这段代码要算什么（一句话，便于审计）")] = "",
    data_files: Annotated[dict[str, str] | None,
                          Field(default=None,
                                description='输入数据：{"文件名": "文件内容"}，沙箱里以只读方式挂到 /data/文件名')] = None,
    timeout: Annotated[float, Field(ge=1, le=300, description="执行超时（秒）")] = 30.0,
) -> dict:
    """在**隔离沙箱**里执行 Python 代码，用来做固定工具覆盖不了的分析（统计、聚合、格式转换）。
    代码运行在无网络、只读文件系统、资源受限的容器里，只能读取通过 data_files 传入的数据。
    典型用法：先用 tail_file / remote_logs 把日志取回来，把内容放进 data_files，
    再写代码统计（例如按分钟统计 5xx 数量）。返回值是 stdout。"""
    settings = get_settings()
    if not settings.sandbox_enabled:
        raise ToolError("沙箱已禁用（SA_SANDBOX_ENABLED=false），无法执行生成的代码")
    if data_files:
        total = sum(len(v) for v in data_files.values())
        if total > 400_000:
            raise ToolError(f"输入数据过大（{total} 字符）：请先在主机侧缩小范围（grep/head）再传入")
    try:
        sandbox = _make_sandbox()
        result = await sandbox.run(code, files=data_files, timeout=min(timeout, settings.sandbox_timeout * 4))
    except SandboxError as e:
        raise ToolError(f"沙箱不可用：{e}") from e
    if not result.ok and "超时" in result.stderr:
        raise ToolError(result.stderr)
    return {"backend": result.backend, "ok": result.ok, "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms, "purpose": purpose,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:1500] if not result.ok else ""}
