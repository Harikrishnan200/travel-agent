"""What happens when an MCP server, or Redis, is unavailable.

The rule: a partial outage degrades the plan and says so; a total outage on a
leg of the trip fails the plan rather than presenting an impossible one.
"""

from unittest.mock import patch

import pytest

from app import cache
from app.graph.graph import build_graph
from app.graph.state import new_state
from gateway import mcp_router, service

TRANSPORT_TOOLS = {"search_flights", "search_trains", "search_buses"}


@pytest.fixture
def plan_with_tools_down():
    """Run a full trip with a chosen set of tools failing as if their server were down."""
    working = mcp_router.call_tool

    def run(down: set[str], thread: str = "t", **state_overrides):
        def flaky(tool, arguments):
            if tool in down:
                raise mcp_router.MCPUnavailableError(f"{tool} server unreachable")
            return working(tool, arguments)

        state = new_state(thread, "Plan a trip from Kochi to Delhi")
        state.update(state_overrides)
        with patch.object(mcp_router, "call_tool", flaky):
            return build_graph().invoke(state, config={"configurable": {"thread_id": thread}})

    return run


# --- partial outages: carry on, and say what was missed ---------------------


def test_one_transport_mode_down_still_produces_a_plan(plan_with_tools_down):
    result = plan_with_tools_down({"search_trains"}, "trains-down")
    assert result["status"] == "awaiting_approval"
    assert result["selected_transport"]["mode"] in {"flight", "bus"}
    assert "train search unavailable" in result["degraded"]


def test_weather_down_still_produces_an_itinerary(plan_with_tools_down):
    result = plan_with_tools_down({"get_weather"}, "weather-down")
    assert result["status"] == "awaiting_approval"
    assert len(result["itinerary"]["days"]) == result["duration_days"]
    assert result["weather"] == []
    assert any("weather unavailable" in note for note in result["degraded"])


def test_route_lookup_down_does_not_stop_planning(plan_with_tools_down):
    result = plan_with_tools_down({"calculate_route"}, "route-down")
    assert result["status"] == "awaiting_approval"
    assert "route calculation unavailable" in result["degraded"]


# --- total outage on a leg: fail honestly, do not invent a plan -------------


def test_all_transport_down_fails_the_plan(plan_with_tools_down):
    """Without this the plan looks fine but has no way to get there."""
    result = plan_with_tools_down(TRANSPORT_TOOLS, "no-transport")
    assert result["status"] == "needs_user_decision"
    assert any("No transport" in error for error in result["validation_errors"])


def test_hotels_down_fails_the_plan(plan_with_tools_down):
    result = plan_with_tools_down({"search_hotels"}, "no-hotel")
    assert result["status"] == "needs_user_decision"
    assert any("No accommodation" in error for error in result["validation_errors"])


def test_an_outage_is_not_reported_as_a_budget_problem(plan_with_tools_down):
    """Telling someone to raise their budget when a provider is down is misleading."""
    result = plan_with_tools_down(TRANSPORT_TOOLS, "outage-message")
    response = result["final_response"]
    assert "did not return any options" in response
    assert "increase the budget" not in response
    assert "unavailable" in response


def test_a_missing_leg_is_never_silently_priced_as_free(plan_with_tools_down):
    result = plan_with_tools_down(TRANSPORT_TOOLS, "no-free-lunch")
    assert result["cost_breakdown"]["transport"] == 0
    assert result["status"] != "awaiting_approval"


# --- caching ----------------------------------------------------------------


def test_an_identical_call_is_served_from_cache():
    arguments = {"city": "Delhi", "date": "2026-09-18"}
    first = service.execute_tool("experience_agent", "get_weather", arguments)

    with patch.object(mcp_router, "call_tool", side_effect=AssertionError("should not be called")):
        second = service.execute_tool("experience_agent", "get_weather", arguments)

    assert second == first


def test_different_arguments_are_cached_separately():
    key_one = cache.make_key("get_weather", {"city": "Delhi", "date": "2026-09-18"})
    key_two = cache.make_key("get_weather", {"city": "Delhi", "date": "2026-09-19"})
    assert key_one != key_two


def test_argument_order_does_not_change_the_cache_key():
    assert cache.make_key("get_weather", {"city": "Delhi", "date": "x"}) == cache.make_key(
        "get_weather", {"date": "x", "city": "Delhi"}
    )


def test_each_tool_has_a_sensible_cache_lifetime():
    assert cache.ttl_for("search_places") > cache.ttl_for("get_weather")
    assert cache.ttl_for("get_weather") > cache.ttl_for("search_flights")


def test_a_failed_call_is_not_cached():
    arguments = {"origin": "Kochi", "destination": "Delhi", "date": "2026-09-18"}
    with patch.object(mcp_router, "call_tool", side_effect=mcp_router.MCPUnavailableError("down")):
        with pytest.raises(mcp_router.MCPUnavailableError):
            service.execute_tool("transport_agent", "search_flights", arguments)

    assert cache.get(cache.make_key("search_flights", arguments)) is None


# --- Redis itself being down -------------------------------------------------


def test_planning_works_with_no_redis(no_redis):
    result = build_graph().invoke(
        new_state("no-redis", "Kochi to Delhi"), config={"configurable": {"thread_id": "no-redis"}}
    )
    assert result["status"] == "awaiting_approval"


def test_rate_limiting_still_applies_with_no_redis(no_redis, monkeypatch):
    """Losing Redis must not quietly remove the limit."""
    monkeypatch.setattr(service.settings, "tool_rate_limit_per_minute", 2)
    arguments = {"city": "Delhi", "date": "2026-09-18"}

    for _ in range(2):
        service.execute_tool("experience_agent", "get_weather", arguments)

    with pytest.raises(service.RateLimitExceeded):
        service.execute_tool("experience_agent", "get_weather", arguments)


def test_cache_reads_and_writes_are_safe_with_no_redis(no_redis):
    cache.set("tool:whatever", {"a": 1}, 60)
    assert cache.get("tool:whatever") is None
