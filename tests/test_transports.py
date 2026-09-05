"""The HTTP paths: agent to gateway, and gateway to MCP server.

These only run when SPLIT_PROCESSES is true, which is how docker-compose runs
the system - so without these tests the deployed configuration is the one with
no coverage. The network itself is faked; what is under test is the request we
build and how we read the answer back.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import mcp_client
from app.mcp_client import ToolCallFailed
from gateway import mcp_router
from gateway.mcp_router import MCPToolError, MCPUnavailableError
from gateway.policy import ToolNotAllowedError

FLIGHT_ARGS = {"origin": "Kochi", "destination": "Delhi", "date": "2026-09-19"}


@pytest.fixture
def split_processes(monkeypatch):
    monkeypatch.setattr(mcp_client.settings, "split_processes", True)
    monkeypatch.setattr(mcp_router.settings, "split_processes", True)


def response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=payload,
        request=httpx.Request("POST", "http://gateway/call"),
    )


# --- agent to gateway, over HTTP --------------------------------------------


def test_a_successful_call_returns_just_the_result(split_processes):
    with patch.object(httpx, "post", return_value=response(200, {"result": [{"id": "6E-1"}]})):
        assert mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS) == [
            {"id": "6E-1"}
        ]


def test_the_api_key_and_agent_name_are_sent(split_processes):
    """The gateway authenticates on one and authorizes on the other."""
    with patch.object(httpx, "post", return_value=response(200, {"result": []})) as post:
        mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS, trip_id="t-1")

    headers = post.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == mcp_client.settings.mcp_gateway_api_key
    assert headers["X-Agent-Name"] == "transport_agent"
    assert post.call_args.kwargs["json"]["trip_id"] == "t-1"
    assert post.call_args.kwargs["json"]["tool"] == "search_flights"


def test_a_403_is_an_authorization_error_not_a_generic_failure(split_processes):
    """A refused tool is a bug in our wiring, and must not look like an outage."""
    refusal = response(403, {"detail": "Agent 'transport_agent' is not allowed to call x"})
    with patch.object(httpx, "post", return_value=refusal):
        with pytest.raises(ToolNotAllowedError, match="not allowed"):
            mcp_client.call_tool("transport_agent", "search_hotels", {})


def test_an_unreachable_gateway_is_a_tool_failure(split_processes):
    """The agent degrades rather than crashing the whole plan."""
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(ToolCallFailed, match="unreachable"):
            mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_other_errors_are_reported_with_their_status(split_processes, status):
    with patch.object(httpx, "post", return_value=response(status, {"detail": "nope"})):
        with pytest.raises(ToolCallFailed, match=str(status)):
            mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS)


def test_a_non_json_error_body_does_not_mask_the_failure(split_processes):
    broken = httpx.Response(
        500, text="<html>gateway down</html>", request=httpx.Request("POST", "http://gateway/call")
    )
    with patch.object(httpx, "post", return_value=broken):
        with pytest.raises(ToolCallFailed):
            mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS)


def test_in_process_mode_does_not_touch_the_network():
    """The default path must not depend on a gateway being up."""
    with patch.object(httpx, "post", side_effect=AssertionError("should not be called")):
        assert mcp_client.call_tool("transport_agent", "search_flights", FLIGHT_ARGS)


# --- gateway to MCP server ---------------------------------------------------


def test_every_tool_routes_to_a_known_server():
    for tool, server in mcp_router.TOOL_SERVER.items():
        assert mcp_router.server_url(server).startswith("http"), tool


def test_an_unreachable_mcp_server_is_reported_as_unavailable(split_processes):
    """503 to the agent, so it carries on with whatever else worked."""
    with patch.object(mcp_router, "streamablehttp_client", create=True):
        with patch(
            "mcp.client.streamable_http.streamablehttp_client",
            side_effect=OSError("connection refused"),
        ):
            with pytest.raises(MCPUnavailableError, match="unreachable"):
                mcp_router.call_tool("search_flights", FLIGHT_ARGS)


def test_a_tool_error_from_the_server_is_not_an_outage():
    """Bad arguments are the caller's fault; the server is fine."""
    with pytest.raises(MCPToolError):
        mcp_router.call_tool(
            "search_flights", {"origin": "Atlantis", "destination": "Delhi", "date": "2026-09-19"}
        )


def test_calling_from_async_code_is_refused_clearly():
    """The synchronous entry point cannot run inside a loop, and says so."""

    async def inside_a_loop():
        return mcp_router.call_tool("get_weather", {"city": "Delhi", "date": "2026-09-19"})

    import asyncio

    with pytest.raises(RuntimeError, match="synchronous"):
        asyncio.run(inside_a_loop())


# --- reading MCP results back ------------------------------------------------


def test_a_list_result_is_unwrapped_from_its_envelope():
    assert mcp_router._unwrap(([], {"result": [1, 2]})) == [1, 2]


def test_a_dict_result_is_returned_as_it_is():
    assert mcp_router._unwrap(([], {"city": "Delhi"})) == {"city": "Delhi"}


def test_a_result_that_only_has_content_blocks_is_parsed():
    block = MagicMock()
    block.text = json.dumps({"city": "Delhi"})
    assert mcp_router._unwrap([block]) == {"city": "Delhi"}


def test_an_empty_result_is_passed_through():
    assert mcp_router._unwrap([]) == []
