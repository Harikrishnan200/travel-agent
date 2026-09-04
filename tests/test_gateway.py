"""MCP Gateway: authentication, authorization, rate limiting, routing, HITL.

These drive the real FastAPI app, so status codes and headers are exactly what
an agent would see over the wire.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from gateway import service
from gateway.mcp_router import MCPToolError, MCPUnavailableError
from gateway.server import app

settings = get_settings()
client = TestClient(app)

AUTH = {"X-API-Key": settings.mcp_gateway_api_key, "X-Agent-Name": "transport_agent"}

FLIGHT_ARGS = {
    "origin": "Kochi",
    "destination": "Delhi",
    "date": "2026-09-18",
    "passengers": 2,
}


def call(tool, arguments=None, headers=AUTH, **extra):
    return client.post(
        "/call", json={"tool": tool, "arguments": arguments or {}, **extra}, headers=headers
    )


# --- health -----------------------------------------------------------------


def test_health_needs_no_credentials():
    assert client.get("/health").status_code == 200


# --- authentication ---------------------------------------------------------


def test_a_missing_api_key_is_rejected():
    response = call("search_flights", FLIGHT_ARGS, headers={"X-Agent-Name": "transport_agent"})
    assert response.status_code == 401


def test_a_wrong_api_key_is_rejected():
    headers = {**AUTH, "X-API-Key": "not-the-key"}
    assert call("search_flights", FLIGHT_ARGS, headers=headers).status_code == 401


def test_authentication_is_checked_before_anything_else():
    """A bad key must not reveal whether the tool or agent even exists."""
    headers = {"X-API-Key": "wrong", "X-Agent-Name": "rogue_agent"}
    assert call("no_such_tool", headers=headers).status_code == 401


# --- agent identity and authorization ---------------------------------------


def test_an_unknown_agent_is_rejected():
    headers = {**AUTH, "X-Agent-Name": "rogue_agent"}
    assert call("search_flights", FLIGHT_ARGS, headers=headers).status_code == 403


def test_an_agent_may_call_its_own_tools():
    response = call("search_flights", FLIGHT_ARGS)
    assert response.status_code == 200
    assert response.json()["result"]


def test_an_agent_may_not_call_another_agents_tools():
    """The point of the gateway: transport asking for hotels is refused."""
    response = call("search_hotels", {"city": "Delhi", "check_in": "x", "check_out": "y"})
    assert response.status_code == 403
    assert "not allowed" in response.json()["detail"]


def test_the_travel_manager_cannot_call_any_tool():
    headers = {**AUTH, "X-Agent-Name": "travel_manager"}
    assert call("search_flights", FLIGHT_ARGS, headers=headers).status_code == 403


def test_an_unknown_tool_is_rejected():
    assert call("drop_database", {}).status_code == 403


# --- tool discovery ---------------------------------------------------------


def test_discovery_returns_only_this_agents_tools():
    response = client.get("/tools", headers=AUTH)
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["tools"]}
    assert names == {"search_flights", "search_trains", "search_buses"}


def test_discovery_marks_which_tools_need_approval(monkeypatch):
    from gateway import policy

    monkeypatch.setitem(
        policy.AGENT_TOOL_ACCESS, "transport_agent", {"search_flights", "book_flight"}
    )
    tools = {t["name"]: t for t in client.get("/tools", headers=AUTH).json()["tools"]}
    assert tools["search_flights"]["requires_approval"] is False
    assert tools["book_flight"]["requires_approval"] is True


def test_discovery_differs_per_agent():
    headers = {**AUTH, "X-Agent-Name": "experience_agent"}
    names = {t["name"] for t in client.get("/tools", headers=headers).json()["tools"]}
    assert names == {"search_places", "get_weather", "calculate_route"}


def test_discovery_needs_authentication():
    assert client.get("/tools", headers={"X-Agent-Name": "transport_agent"}).status_code == 401


# --- human in the loop for action tools -------------------------------------


def test_a_booking_tool_is_refused_without_approval():
    """No agent is granted booking tools, so authorization refuses it first."""
    assert call("book_flight", {}).status_code == 403


def test_approval_is_required_even_for_an_authorized_action_tool(monkeypatch):
    """If an agent were ever granted a booking tool, it would still need a human."""
    from gateway import policy

    monkeypatch.setitem(policy.AGENT_TOOL_ACCESS, "transport_agent", {"book_flight"})
    response = call("book_flight", {})
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "human_approval_required"


# --- rate limiting ----------------------------------------------------------


def test_calls_beyond_the_limit_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "tool_rate_limit_per_minute", 3)
    for _ in range(3):
        assert call("search_flights", FLIGHT_ARGS).status_code == 200

    response = call("search_flights", FLIGHT_ARGS)
    assert response.status_code == 429
    assert "rate limit" in response.json()["detail"].lower()


def test_the_rate_limit_is_per_agent(monkeypatch):
    monkeypatch.setattr(settings, "tool_rate_limit_per_minute", 2)
    for _ in range(2):
        call("search_flights", FLIGHT_ARGS)
    assert call("search_flights", FLIGHT_ARGS).status_code == 429

    experience = {**AUTH, "X-Agent-Name": "experience_agent"}
    other = call("get_weather", {"city": "Delhi", "date": "2026-09-18"}, headers=experience)
    assert other.status_code == 200


# --- failures from the MCP servers ------------------------------------------


def test_bad_tool_arguments_come_back_as_a_client_error():
    response = call("search_flights", {**FLIGHT_ARGS, "date": "18-09-2026"})
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_an_unreachable_mcp_server_is_a_503(monkeypatch):
    def unreachable(tool, arguments):
        raise MCPUnavailableError("transport MCP server is unreachable")

    monkeypatch.setattr(service.mcp_router, "call_tool", unreachable)
    response = call("search_flights", FLIGHT_ARGS)
    assert response.status_code == 503


def test_a_tool_that_raises_is_a_400_not_a_500(monkeypatch):
    def broken(tool, arguments):
        raise MCPToolError("Unknown city: 'Atlantis'")

    monkeypatch.setattr(service.mcp_router, "call_tool", broken)
    assert call("search_flights", FLIGHT_ARGS).status_code == 400


# --- the service function behind the endpoint -------------------------------


def test_execute_tool_refuses_an_unauthorized_agent():
    from gateway.policy import ToolNotAllowedError

    with pytest.raises(ToolNotAllowedError):
        service.execute_tool("accommodation_agent", "search_flights", FLIGHT_ARGS)


def test_execute_tool_returns_the_same_data_the_endpoint_does():
    direct = service.execute_tool("transport_agent", "search_flights", FLIGHT_ARGS)
    over_http = call("search_flights", FLIGHT_ARGS).json()["result"]
    assert direct == over_http
