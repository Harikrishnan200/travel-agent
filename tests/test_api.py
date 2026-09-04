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


# --- trip planning ---------------------------------------------------------


def test_create_trip_returns_a_plan_awaiting_approval():
    response = client.post(
        "/api/v1/trips",
        json={"message": "Plan a 5 day trip from Kochi to Delhi for 2 people"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["plan"]["itinerary"]["days"]
    assert body["total_cost"] == sum(body["plan"]["cost_breakdown"].values())


def test_created_trip_can_be_fetched_again():
    trip_id = client.post("/api/v1/trips", json={"message": "Kochi to Delhi"}).json()["trip_id"]
    fetched = client.get(f"/api/v1/trips/{trip_id}").json()
    assert fetched["trip_id"] == trip_id
    assert fetched["status"] == "awaiting_approval"


def test_unknown_trip_returns_404():
    assert client.get("/api/v1/trips/does-not-exist").status_code == 404


def test_empty_message_is_rejected():
    assert client.post("/api/v1/trips", json={"message": ""}).status_code == 422


def test_response_does_not_expose_raw_tool_payloads():
    body = client.post("/api/v1/trips", json={"message": "Kochi to Delhi"}).json()
    assert "transport_options" not in body["plan"]
    assert "hotel_options" not in body["plan"]
