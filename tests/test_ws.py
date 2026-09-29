"""WebSocket 通道测试。"""

import json

from fastapi.testclient import TestClient

from server_agent.agent.loop import Agent
from server_agent.config import Settings
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.server.app import create_app
from server_agent.tools.registry import ToolRegistry


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"mount": path, "percent": 97}

    return reg


def app_with(api_token=None):
    tools = make_registry()

    def factory():
        llm = MockLLM([tool_call("disk_usage", {}), "根分区 97%"])
        return Agent(llm, tools, stream=False)

    return create_app(Settings(api_token=api_token), agent_factory=factory, tools_registry=tools)


def recv_until(ws, wanted, limit=50):
    """读到出现某个事件类型为止，返回读到的所有消息。"""
    seen = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        seen.append(msg)
        if msg.get("type") == wanted:
            return seen
    raise AssertionError(f"没等到 {wanted}，实际收到 {[m.get('type') for m in seen]}")


def test_ws_ask_receives_events_and_pong():
    with TestClient(app_with()) as c:
        with c.websocket_connect("/ws") as ws:
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and "disk_usage" in ready["tools"]

            ws.send_text(json.dumps({"type": "ping"}))
            assert json.loads(ws.receive_text())["type"] == "pong"

            ws.send_text(json.dumps({"type": "ask", "input": "磁盘满了吗"}))
            accepted = json.loads(ws.receive_text())
            assert accepted["type"] == "accepted" and accepted["run_id"].startswith("run_")

            msgs = recv_until(ws, "end")
            types = [m["type"] for m in msgs]
            assert types[0] == "start" and "tool_call" in types and "tool_result" in types
            assert msgs[-1]["data"]["text"] == "根分区 97%"


def test_ws_bad_messages():
    with TestClient(app_with()) as c:
        with c.websocket_connect("/ws") as ws:
            ws.receive_text()
            ws.send_text("不是 json")
            assert "合法 JSON" in json.loads(ws.receive_text())["message"]
            ws.send_text(json.dumps({"type": "ask", "input": "   "}))
            assert "input 不能为空" in json.loads(ws.receive_text())["message"]
            ws.send_text(json.dumps({"type": "explode"}))
            assert "未知消息类型" in json.loads(ws.receive_text())["message"]
            ws.send_text(json.dumps({"type": "cancel", "run_id": "run_none"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "cancelled" and msg["ok"] is False


def test_ws_requires_token_when_enabled():
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app_with(api_token="s3cret")) as c:
        try:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
            raise AssertionError("未带 token 竟然连上了")
        except WebSocketDisconnect as e:
            assert e.code == 4401
        with c.websocket_connect("/ws?token=s3cret") as ws:
            assert json.loads(ws.receive_text())["type"] == "ready"
