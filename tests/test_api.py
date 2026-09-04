"""API endpoint tests. Phase 1 covers health and root only."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["use_mock_data"] is True


def test_health_does_not_leak_secrets():
    body = client.get("/health").json()
    for key in body:
        assert "key" not in key.lower()
        assert "url" not in key.lower()


def test_root_returns_service_info():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "ai-travel-planning-agent"
