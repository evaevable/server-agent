"""MCP Server / Client 测试。

分两层：
1. **进程内**：直接测 JSON-RPC handler（快、稳定）；
2. **跨进程**：用真的 MCPClient 起一个子进程，验证 stdio 传输与握手（接近真实使用）。
"""

import asyncio
import json
import shutil
import sys

import pytest

from server_agent.mcp import MCPClient, MCPToolInfo, make_server, register_mcp_tools
from server_agent.policy import AuditLog
from server_agent.tools.registry import ToolRegistry


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="host_info")
    def _host() -> dict:
        """看主机信息。"""
        return {"hostname": "test-host", "os": "Linux"}

    @reg.tool(name="remember_fact", risk="low")
    def _remember(host: str, key: str, value: str) -> dict:
        """记一条事实。"""
        return {"saved": True, "host": host, "key": key}

    @reg.tool(name="restart_service", risk="high")
    def _restart(name: str, dry_run: bool = True) -> dict:
        """重启服务（高危）。"""
        return {"dry_run": dry_run, "action": f"restart {name}"}

    return reg


def call(handler, method, params=None, rid=1):
    return asyncio.run(handler({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))


# ---------- 进程内：协议与暴露策略 ----------

def test_initialize_and_ping():
    handler, _ = make_server(make_registry(), audit=AuditLog(None))
    init = call(handler, "initialize")["result"]
    assert init["protocolVersion"] and init["serverInfo"]["name"] == "server-agent"
    assert init["capabilities"]["tools"] == {"listChanged": False}
    assert call(handler, "ping")["result"] == {}
    assert call(handler, "notifications/initialized") is None       # 通知无响应
    assert call(handler, "unknown/method")["error"]["code"] == -32601


def test_tools_list_exposes_only_read_by_default():
    handler, _ = make_server(make_registry(), audit=AuditLog(None))
    tools = call(handler, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"host_info", "remember_fact"}                  # 高危工具默认不暴露
    host = next(t for t in tools if t["name"] == "host_info")
    assert host["description"] and host["inputSchema"]["type"] == "object"


def test_tools_list_with_expose_write():
    handler, _ = make_server(make_registry(), expose_write=True, audit=AuditLog(None))
    names = {t["name"] for t in call(handler, "tools/list")["result"]["tools"]}
    assert names == {"host_info", "remember_fact", "restart_service"}


def test_tools_call_read_tool_returns_content():
    handler, _ = make_server(make_registry(), audit=AuditLog(None))
    result = call(handler, "tools/call", {"name": "host_info", "arguments": {}})["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["hostname"] == "test-host"


def test_tools_call_high_risk_is_denied_without_approver():
    """MCP 场景没有交互式审批人：高危操作必须被拒绝（默认拒绝原则的延伸）。"""
    handler, _ = make_server(make_registry(), expose_write=True, audit=AuditLog(None))
    result = call(handler, "tools/call", {"name": "restart_service", "arguments": {"name": "nginx"}})["result"]
    assert result["isError"] is True and "审批" in result["content"][0]["text"]


def test_tools_call_unknown_or_unexposed_tool():
    handler, _ = make_server(make_registry(), audit=AuditLog(None))
    r1 = call(handler, "tools/call", {"name": "nope", "arguments": {}})["result"]
    assert r1["isError"] is True and "不可用或未暴露" in r1["content"][0]["text"]
    r2 = call(handler, "tools/call", {"name": "restart_service", "arguments": {}})["result"]
    assert r2["isError"] is True and "不可用或未暴露" in r2["content"][0]["text"]


def test_audit_records_mcp_calls():
    audit = AuditLog(None)
    handler, _ = make_server(make_registry(), audit=audit)
    call(handler, "tools/call", {"name": "host_info", "arguments": {}})
    assert any(r["run_id"] == "mcp" and r["tool"] == "host_info" for r in audit.records())


# ---------- 注册外部工具 ----------

def test_register_mcp_tools_defaults_to_high_risk():
    reg = ToolRegistry()
    tools = [MCPToolInfo("query_metrics", "查指标", {"type": "object",
                                                   "properties": {"sql": {"type": "string"}}})]
    client = MCPClient("true")          # 只用于闭包，不会真的启动
    names = register_mcp_tools(reg, tools, client=client, prefix="mcp_prom_")
    assert names == ["mcp_prom_query_metrics"]
    entry = reg.get("mcp_prom_query_metrics")
    assert entry.risk == "high"                                  # 不了解远端，默认按高危
    assert "sql" in entry.schema()["function"]["parameters"]["properties"]


async def test_registered_mcp_tool_calls_remote_client(monkeypatch):
    reg = ToolRegistry()
    tools = [MCPToolInfo("ping_tool", "远端探活", {"type": "object", "properties": {}})]

    class FakeClient:
        async def call_tool(self, name, arguments):
            return {"is_error": False, "text": f"远端收到 {name} {arguments}"}

    register_mcp_tools(reg, tools, client=FakeClient(), prefix="x_", risk="read")
    r = await reg.call("x_ping_tool", {})
    assert r.ok and "远端收到 ping_tool" in r.content


# ---------- 跨进程：真实 stdio 传输 ----------

def python_executable() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def test_stdio_round_trip_via_real_subprocess():
    command = f"{python_executable()} -m server_agent.mcp.server"

    async def run():
        client = MCPClient(command, timeout=30)
        try:
            info = await client.start()
            assert info["serverInfo"]["name"] == "server-agent"
            tools = await client.list_tools()
            names = {t.name for t in tools}
            assert "host_info" in names and "restart_service" not in names   # 默认不暴露写操作
            result = await client.call_tool("host_info", {})
            assert result["is_error"] is False
            assert "hostname" in json.loads(result["text"])
            bad = await client.call_tool("restart_service", {"name": "nginx"})
            assert bad["is_error"] is True
            return True
        finally:
            await client.close()

    assert asyncio.run(run()) is True


def test_client_reports_close_on_bad_command():
    async def run():
        client = MCPClient(f"{python_executable()} -c 'import sys; sys.exit(1)'", timeout=5)
        await client.start()
        with pytest.raises(Exception):
            await client.list_tools()
        await client.close()

    with pytest.raises(Exception):
        asyncio.run(run())
