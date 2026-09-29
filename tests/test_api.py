"""HTTP 接口测试：用 TestClient + MockLLM agent_factory，全程离线。"""

import json

import pytest
from fastapi.testclient import TestClient

from server_agent.agent.loop import Agent
from server_agent.config import Settings
from server_agent.llm.mock import MockLLM, tool_call
from server_agent.server.app import create_app
from server_agent.tools.registry import ToolRegistry


def make_tools() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="disk_usage")
    def _disk(path: str = "/") -> dict:
        """查磁盘。"""
        return {"mount": path, "percent": 97}

    return reg


def factory(_llm_script=None):
    def build():
        llm = MockLLM([tool_call("disk_usage", {}), "根分区 97%，建议清理"])
        return Agent(llm, make_tools(), stream=False)
    return build


def client(**settings_kw) -> TestClient:
    tools = make_tools()
    return TestClient(create_app(Settings(**settings_kw), agent_factory=factory(), tools_registry=tools))


def read_all(run_id, c):
    with c.stream("GET", f"/api/runs/{run_id}/events") as r:
        events = read_sse(r)
    from server_agent.agent.events import Event

    return [Event(e["event"], run_id, e["id"], e["data"]) for e in events]


def read_sse(resp) -> list[dict]:
    """把 SSE 响应解析成 [{id, event, data}]。"""
    out, cur = [], {}
    for raw in resp.iter_lines():
        line = raw.strip()
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith("id: "):
            cur["id"] = int(line[4:])
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    if cur:
        out.append(cur)
    return out


def test_health_is_public_and_reports_auth_state():
    body = client(api_token="s3cret").get("/health").json()
    assert body["status"] == "ok" and body["auth"] is True


def test_create_run_and_read_events():
    with client() as c:
        r = c.post("/api/runs", json={"input": "磁盘满了吗"})
        assert r.status_code == 202
        created = r.json()
        assert created["status"] == "running" and created["events_url"].endswith("/events")

        with c.stream("GET", created["events_url"]) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache"
            events = read_sse(resp)

        types = [e["event"] for e in events]
        assert types[0] == "start" and types[-1] == "end"
        assert [e["id"] for e in events] == list(range(1, len(events) + 1))
        assert events[-1]["data"]["text"] == "根分区 97%，建议清理"

        detail = c.get(f"/api/runs/{created['id']}").json()
        assert detail["status"] == "done" and detail["result"]["tool_calls"] == 1


def test_events_resume_with_last_event_id():
    with client() as c:
        run_id = c.post("/api/runs", json={"input": "磁盘"}).json()["id"]
        with c.stream("GET", f"/api/runs/{run_id}/events") as r1:
            read_sse(r1)
        # 断线重连：只拿 seq > 2 的事件
        with c.stream("GET", f"/api/runs/{run_id}/events",
                      headers={"Last-Event-ID": "2"}) as r2:
            resumed = read_sse(r2)
        all_ids = [e.seq for e in read_all(run_id, c)]
        assert [e["id"] for e in resumed] == all_ids[2:]  # 只少了前两个（start / step）


def test_list_runs_and_tools():
    with client() as c:
        assert c.get("/api/runs").json() == {"count": 0, "runs": []}
        c.post("/api/runs", json={"input": "一"})
        c.post("/api/runs", json={"input": "二"})
        listed = c.get("/api/runs").json()
        assert listed["count"] == 2 and listed["runs"][0]["input"] == "二"  # 新的在前
        tools = c.get("/api/tools").json()
        assert tools["count"] == 1 and tools["tools"][0]["name"] == "disk_usage"


def test_cancel_and_404s():
    with client() as c:
        run_id = c.post("/api/runs", json={"input": "磁盘"}).json()["id"]
        r = c.post(f"/api/runs/{run_id}/cancel")
        assert r.status_code == 200 and r.json()["id"] == run_id
        assert c.get("/api/runs/run_nope").status_code == 404
        assert c.post("/api/runs/run_nope/cancel").status_code == 404
        assert c.get("/api/runs/run_nope/events").status_code == 404


def test_input_validation():
    with client() as c:
        assert c.post("/api/runs", json={"input": ""}).status_code == 422
        assert c.post("/api/runs", json={}).status_code == 422
        assert c.post("/api/runs", json={"input": "x" * 5000}).status_code == 422


@pytest.mark.parametrize("method,path", [
    ("post", "/api/runs"),
    ("get", "/api/runs"),
    ("get", "/api/tools"),
])
def test_auth_required_when_token_set(method, path):
    with client(api_token="s3cret") as c:
        call = getattr(c, method)
        body = {"input": "磁盘"} if method == "post" else None
        r = call(path, json=body) if body else call(path)
        assert r.status_code == 401 and "Bearer" in r.headers.get("www-authenticate", "")
        ok = call(path, json=body, headers={"Authorization": "Bearer s3cret"}) if body else \
            call(path, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code in (200, 202)


def test_auth_accepts_empty_when_disabled():
    with client() as c:
        assert c.get("/api/runs").status_code == 200
