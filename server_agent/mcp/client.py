"""MCP Client：把外部 MCP Server 的工具接进我们的注册表。

用途：别人的 MCP Server（数据库查询、K8s 操作、内部平台 API）可以像本地工具一样被 Agent 使用。

两个必须守住的边界：
1. **外部工具也走策略层**：远端返回的工具默认按 `high` 风险对待（我们不了解它做什么），
   除非显式声明为只读；否则等于给自己开了一个绕过审批的后门。
2. **命名空间隔离**：注册进注册表时加前缀（默认 `mcp_<server>_`），避免与本地工具同名冲突。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass
from typing import Any

from server_agent.tools.registry import ToolRegistry


@dataclass
class MCPToolInfo:
    name: str
    description: str
    input_schema: dict


class MCPClientError(RuntimeError):
    pass


class MCPClient:
    """极简 MCP 客户端：stdio + JSON-RPC。

    只实现项目需要的方法集（initialize / tools/list / tools/call）。
    """

    def __init__(self, command: str, *, timeout: float = 20.0):
        self.command = command
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def start(self) -> dict:
        argv = shlex.split(self.command)
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        result = await self._request("initialize", {"protocolVersion": "2024-11-05",
                                                    "capabilities": {}, "clientInfo": {"name": "server-agent"}})
        await self._notify("notifications/initialized")
        return result

    async def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPClientError("MCP 进程未启动")
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_message(self) -> dict:
        if self._proc is None or self._proc.stdout is None:
            raise MCPClientError("MCP 进程未启动")
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
        if not line:
            raise MCPClientError("MCP 进程关闭了连接")
        return json.loads(line.decode())

    async def _request(self, method: str, params: dict | None = None) -> dict:
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            for _ in range(20):                     # 跳过通知类消息
                msg = await self._read_message()
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise MCPClientError(f"{method} 失败: {msg['error']}")
                    return msg.get("result") or {}
            raise MCPClientError(f"{method} 没有收到对应响应")

    async def _notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def list_tools(self) -> list[MCPToolInfo]:
        result = await self._request("tools/list")
        return [MCPToolInfo(t.get("name", ""), t.get("description", ""), t.get("inputSchema") or {})
                for t in result.get("tools", [])]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        texts = [c.get("text", "") for c in (result.get("content") or []) if c.get("type") == "text"]
        return {"is_error": bool(result.get("isError")), "text": "\n".join(texts)}

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None


def register_mcp_tools(registry: ToolRegistry, tools: list[MCPToolInfo], *, client: "MCPClient",
                       prefix: str, risk: str = "high"):
    """把外部工具注册进注册表。

    risk 默认 high：**我们不了解远端工具做什么**，因此必须经审批；
    只有明确知道它是只读的（例如对方文档声明）才应该传 risk="read"。
    """
    from server_agent.tools.registry import Tool

    def make_callable(info: MCPToolInfo, client: MCPClient):
        async def _call(**kwargs) -> dict:
            return await client.call_tool(info.name, kwargs)

        return _call

    # 注册表要求用类型注解生成 schema；外部 schema 是 JSON，无法直接映射成 Python 类型。
    # 这里用一个「通用参数模型」承载任意字段：把外部 schema 的 properties 原样保留给模型看，
    # 参数校验交给远端服务（它才是权威）。这是 MCP 场景下务实的取舍。
    from pydantic import BaseModel, ConfigDict

    registered = []
    for info in tools:
        props = info.input_schema.get("properties") or {}
        model = type(f"{prefix}{info.name}_params", (BaseModel,),
                     {"model_config": ConfigDict(extra="allow"),
                      "__annotations__": {k: object for k in props}})
        entry = Tool(name=f"{prefix}{info.name}", description=info.description or f"外部 MCP 工具 {info.name}",
                     func=make_callable(info, client), params=model,
                     risk=risk, max_chars=6000)
        registry.register(entry)
        registered.append(entry.name)
    return registered
