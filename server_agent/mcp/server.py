"""把工具注册表暴露成 MCP Server（stdio 传输）。

MCP（Model Context Protocol）解决的是**工具与宿主解耦**：
工具实现一次，任何 MCP 客户端（IDE、Agent 平台、桌面应用）都能调用。

本文件实现 MCP 的 stdio 传输与最小必要方法集：
    initialize              握手，返回协议版本与能力
    notifications/initialized  客户端就绪通知（无响应）
    tools/list              列出工具（name/description/inputSchema）
    tools/call              调用工具，返回 content 数组
    ping                    探活

三条与策略层一致的规矩：
1. **默认只暴露只读工具**（read / low）。写操作要显式 `--expose-write` 才出现——
   fail-closed：不小心暴露的危害远大于少暴露。
2. **策略层照旧生效**：MCP 只是另一个入口，参数校验、审计一样走。
3. **MCP 上下文里没有交互式审批人 → 高危调用按拒绝处理**。
   这是「默认拒绝」在跨进程场景下的自然延伸：没人能点「批准」，那就不能执行。

协议要点：stdio 传输用「一行一个 JSON-RPC 消息」（换行分隔），
这与 HTTP 传输的 SSE 不同，注意不要混淆。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from server_agent import __version__
from server_agent.policy import AuditLog, Policy, get_audit
from server_agent.tools import registry as default_registry

PROTOCOL_VERSION = "2024-11-05"      # 写作时对齐的 MCP 修订版；接入前请核对当前规范
SERVER_NAME = "server-agent"
EXPOSED_RISKS = ("read", "low")


def make_server(registry=None, *, expose_write: bool = False, audit: AuditLog | None = None):
    reg = registry or default_registry
    audit = audit if audit is not None else get_audit()
    policy = Policy()
    allowed = None if expose_write else EXPOSED_RISKS

    def visible_tools() -> list:
        return [t for t in reg.list() if allowed is None or t.risk in allowed]

    async def handle(msg: dict) -> dict | None:
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            tools = [{"name": t.name, "description": t.description,
                      "inputSchema": t.schema()["function"]["parameters"]} for t in visible_tools()]
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            tool = reg.get(name)
            if tool is None or tool not in visible_tools():
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"工具不可用或未暴露：{name}"}], "isError": True}}
            # 没有交互式审批人：high 风险操作会被策略层拦下（默认拒绝）
            result = await reg.call(name, args, policy=policy, approver=None,
                                    audit=audit, run_id="mcp")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": result.content}],
                "isError": not result.ok}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未实现的方法: {method}"}}

    return handle, visible_tools


async def serve_stdio(*, expose_write: bool = False) -> None:
    """从 stdin 读、往 stdout 写。日志一律走 stderr，避免污染协议流。"""
    handle, _ = make_server(expose_write=expose_write)
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "解析失败"}}), flush=True)
            continue
        response = await handle(msg)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    expose_write = "--expose-write" in argv
    if not expose_write:
        print("[mcp] 只暴露只读工具；如需写操作请加 --expose-write（仍会走审批/拒绝）", file=sys.stderr)
    asyncio.run(serve_stdio(expose_write=expose_write))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
