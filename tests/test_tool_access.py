"""Tool authorization: each agent sees only its own domain's tools.

Phase 5 moves enforcement to the MCP Gateway over HTTP; these tests assert the
policy itself, so they keep protecting the same rule afterwards.
"""

import pytest

from app.tools import get_tools
from gateway.mcp_router import TOOL_SERVER
from gateway.policy import (
    ACTION_TOOLS,
    AGENT_TOOL_ACCESS,
    READ_TOOLS,
    ToolNotAllowedError,
    check_access,
    requires_human_approval,
)


def test_each_agent_sees_only_its_own_tools():
    assert set(get_tools("transport_agent")) == {
        "search_flights",
        "search_trains",
        "search_buses",
    }
    assert set(get_tools("accommodation_agent")) == {"search_hotels"}
    assert set(get_tools("experience_agent")) == {
        "search_places",
        "get_weather",
        "calculate_route",
    }


def test_the_travel_manager_has_no_travel_tools():
    """The manager coordinates and delegates; it must not reach a provider itself."""
    assert get_tools("travel_manager") == {}


def test_no_agent_can_reach_another_agents_tools():
    for agent, allowed in AGENT_TOOL_ACCESS.items():
        for tool in READ_TOOLS - allowed:
            with pytest.raises(ToolNotAllowedError):
                check_access(agent, tool)


def test_unknown_agents_and_tools_are_rejected():
    with pytest.raises(ToolNotAllowedError, match="Unknown agent"):
        check_access("rogue_agent", "search_flights")
    with pytest.raises(ToolNotAllowedError, match="Unknown tool"):
        check_access("transport_agent", "drop_database")


def test_the_wrapper_blocks_a_tool_smuggled_between_agents():
    """Even handed the callable directly, the check runs at call time."""
    smuggled = get_tools("accommodation_agent")["search_hotels"]
    transport_tools = {**get_tools("transport_agent"), "search_hotels": smuggled}
    assert "search_hotels" in transport_tools  # the agent could try
    with pytest.raises(ToolNotAllowedError):
        check_access("transport_agent", "search_hotels")


def test_action_tools_are_not_granted_to_any_agent():
    """Booking is never callable without a human, so no agent gets it by default."""
    for allowed in AGENT_TOOL_ACCESS.values():
        assert not (allowed & ACTION_TOOLS)


def test_only_action_tools_require_approval():
    assert all(requires_human_approval(tool) for tool in ACTION_TOOLS)
    assert not any(requires_human_approval(tool) for tool in READ_TOOLS)


def test_every_read_tool_is_routable_to_an_mcp_server():
    assert set(TOOL_SERVER) == READ_TOOLS
