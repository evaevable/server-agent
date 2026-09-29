"""前端静态资源与页面托管测试。"""

from fastapi.testclient import TestClient

from server_agent.agent.loop import Agent
from server_agent.config import Settings
from server_agent.llm.mock import MockLLM
from server_agent.server.app import create_app
from server_agent.tools.registry import ToolRegistry


def client(web_dir=None) -> TestClient:
    def factory():
        return Agent(MockLLM(["ok"]), ToolRegistry(), stream=False)

    return TestClient(create_app(Settings(web_dir=web_dir), agent_factory=factory,
                                 tools_registry=ToolRegistry()))


def test_index_and_assets_are_served():
    with client() as c:
        index = c.get("/")
        assert index.status_code == 200
        assert "server-agent 控制台" in index.text
        assert "/app.js" in index.text and "/style.css" in index.text

        js = c.get("/app.js")
        assert js.status_code == 200 and "EventSource" in js.text
        css = c.get("/style.css")
        assert css.status_code == 200 and "--accent" in css.text


def test_api_routes_not_shadowed_by_static_mount():
    with client() as c:
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/api/runs").status_code == 200
        assert c.get("/api/tools").json()["count"] == 0


def test_unknown_static_path_404():
    with client() as c:
        assert c.get("/nope.html").status_code == 404


def test_web_dir_can_be_missing(tmp_path):
    with client(web_dir=str(tmp_path / "missing")) as c:
        assert c.get("/").status_code == 404      # 目录不存在则不挂载静态资源
        assert c.get("/health").status_code == 200


def test_events_accept_query_token():
    """浏览器 EventSource 无法设置请求头，所以事件流要支持 ?token=。"""
    def factory():
        return Agent(MockLLM(["ok"]), ToolRegistry(), stream=False)

    app = create_app(Settings(api_token="s3cret"), agent_factory=factory, tools_registry=ToolRegistry())
    with TestClient(app) as c:
        headers = {"Authorization": "Bearer s3cret"}
        run_id = c.post("/api/runs", json={"input": "x"}, headers=headers).json()["id"]
        assert c.get(f"/api/runs/{run_id}/events").status_code == 401           # 没 Token
        assert c.get(f"/api/runs/{run_id}/events?token=wrong").status_code == 401
        with c.stream("GET", f"/api/runs/{run_id}/events?token=s3cret") as resp:
            assert resp.status_code == 200 and "event: start" in "".join(resp.iter_text())
