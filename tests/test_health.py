from fastapi.testclient import TestClient

from server_agent import __version__
from server_agent.server.app import create_app


def test_health_ok():
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["uptime_seconds"] >= 0


def test_unknown_route_404():
    assert TestClient(create_app()).get("/nope").status_code == 404
