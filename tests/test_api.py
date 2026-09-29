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


REPORT_JSON = json.dumps({
    "summary": "根分区使用率 97%", "severity": "critical",
    "findings": [{"claim": "分区将满", "evidence": "percent=97"}],
    "root_cause": "/var/log 未轮转", "confidence": "medium", "actions": [], "data_gaps": [],
}, ensure_ascii=False)


def report_factory():
    def build():
        from server_agent.llm.mock import text as _text

        return Agent(MockLLM([tool_call("disk_usage", {}), _text(REPORT_JSON)]), make_tools(), stream=False)
    return build


def test_history_endpoints_persist_runs(tmp_path):
    """服务重启（重建 app）后，历史仍能查到 —— 这就是第 08 章「记忆」的服务端体现。"""
    from server_agent.memory import Store

    store = Store(tmp_path / "api.db")
    tools = make_tools()

    def build():
        return TestClient(create_app(Settings(), agent_factory=report_factory(),
                                     tools_registry=tools, store=store))

    with build() as c:
        run_id = c.post("/api/runs", json={"input": "磁盘为什么满了"}).json()["id"]
        import time as _t
        for _ in range(50):
            if c.get(f"/api/runs/{run_id}").json()["status"] != "running":
                break
            _t.sleep(0.02)
        with c.stream("GET", f"/api/runs/{run_id}/events") as r:
            read_sse(r)

    with build() as c2:          # 新进程/新实例
        hist = c2.get("/api/history").json()
        assert hist["count"] == 1 and hist["runs"][0]["id"] == run_id
        assert hist["runs"][0]["status"] == "done"

        detail = c2.get(f"/api/history/{run_id}").json()
        types = [e["type"] for e in detail["events"]]
        assert types[0] == "start" and "tool_call" in types
        assert detail["run"]["report"]["summary"].startswith("根分区")

        assert c2.get("/api/history/run_nope").status_code == 404
    store.close()


def test_history_empty_when_memory_disabled():
    with client(memory_enabled=False) as c:
        body = c.get("/api/history").json()
        assert body["count"] == 0 and "未启用记忆" in body["hint"]


def test_approval_flow_over_api(tmp_path):
    """高危操作必须经 API 批准才执行；拒绝或超时一律不执行。"""
    from server_agent.policy import ApprovalManager

    executed = []
    from server_agent.tools import ops

    original = ops._service_restart
    ops._service_restart = lambda name: (executed.append(name) or ops.CommandOutcome(True, "ok", 0))

    def ops_factory():
        from server_agent.llm.mock import text as _text
        from server_agent.tools import registry as global_registry

        return Agent(MockLLM([tool_call("restart_service", {"name": "nginx", "dry_run": False}), _text(REPORT_JSON)]),
                     global_registry, stream=False)

    app = create_app(Settings(approval_timeout=0.3, memory_enabled=False), agent_factory=ops_factory,
                     store=None)
    try:
        with TestClient(app) as c:
            run_id = c.post("/api/runs", json={"input": "重启 nginx"}).json()["id"]
            pending = []
            for _ in range(60):          # 等审批请求出现
                data = c.get(f"/api/runs/{run_id}/approvals").json()
                if data["count"]:
                    pending = data["approvals"]
                    break
                import time as _t
                _t.sleep(0.02)
            assert pending, "没有产生审批请求"
            ap = pending[0]
            assert ap["tool"] == "restart_service" and ap["status"] == "pending"
            assert ap["dry_run"]["dry_run"] is True          # 审批前已备好预演结果

            r = c.post(f"/api/runs/{run_id}/approvals/{ap['id']}", json={"approved": True})
            assert r.status_code == 200 and r.json()["approval"]["status"] == "approved"
            assert c.post(f"/api/runs/{run_id}/approvals/{ap['id']}", json={"approved": True}).status_code == 409

            import time as _t
            for _ in range(80):
                if c.get(f"/api/runs/{run_id}").json()["status"] != "running":
                    break
                _t.sleep(0.02)
            assert executed == ["nginx"], "批准后应真正执行一次"

    finally:
        ops._service_restart = original


def test_approval_timeout_denies(tmp_path):
    from server_agent.tools import ops

    executed = []
    original = ops._service_restart
    ops._service_restart = lambda name: (executed.append(name) or ops.CommandOutcome(True, "ok", 0))

    def ops_factory():
        from server_agent.llm.mock import text as _text
        from server_agent.tools import registry as global_registry

        return Agent(MockLLM([tool_call("restart_service", {"name": "nginx", "dry_run": False}), _text(REPORT_JSON)]),
                     global_registry, stream=False)

    app = create_app(Settings(approval_timeout=0.15, memory_enabled=False), agent_factory=ops_factory)
    try:
        with TestClient(app) as c:
            run_id = c.post("/api/runs", json={"input": "重启 nginx"}).json()["id"]
            import time as _t
            for _ in range(120):
                if c.get(f"/api/runs/{run_id}").json()["status"] != "running":
                    break
                _t.sleep(0.02)
            detail = c.get(f"/api/runs/{run_id}").json()
            assert detail["status"] == "done"
            assert not executed, "没人批准时绝不能执行"
    finally:
        ops._service_restart = original


def test_audit_endpoint_records_policy_decisions():
    with client(memory_enabled=False) as c:
        body = c.get("/api/audit").json()
        assert "records" in body
        hint = body.get("hint")
        assert hint is None or "未启用" in hint


def test_approvals_unknown_ids_404():
    with client(memory_enabled=False) as c:
        run_id = c.post("/api/runs", json={"input": "x"}).json()["id"]
        assert c.post(f"/api/runs/{run_id}/approvals/ap_nope", json={"approved": True}).status_code == 404
        assert c.get("/api/runs/run_nope/approvals").json()["count"] == 0
