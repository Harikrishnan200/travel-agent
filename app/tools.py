"""Tool dispatch for the agents.

Phase 3: tools are local function calls into the mock providers.
Phase 5: this module calls the MCP Gateway over HTTP instead. The agents do not
change, because they only ever receive a `{tool_name: callable}` mapping.

Authorization is enforced here with the same policy table the Gateway uses, so
an agent reaching for a tool outside its domain fails now, not only later.
"""

from collections.abc import Callable

from gateway.policy import allowed_tools, check_access
from mcp_servers.hotels.mock_data import search_hotels
from mcp_servers.places.mock_data import calculate_route, search_places
from mcp_servers.transport.mock_data import search_buses, search_flights, search_trains
from mcp_servers.weather.mock_data import get_weather

# tool name -> implementation. The only place tool names meet real functions.
TOOL_IMPLEMENTATIONS: dict[str, Callable] = {
    "search_flights": search_flights,
    "search_trains": search_trains,
    "search_buses": search_buses,
    "search_hotels": search_hotels,
    "search_places": search_places,
    "get_weather": get_weather,
    "calculate_route": calculate_route,
}


def get_tools(agent: str) -> dict[str, Callable]:
    """The tools `agent` is allowed to call, each wrapped in an authorization check.

    Handing back a filtered mapping means an agent cannot even see a tool outside
    its domain; the wrapper is the backstop if one is somehow called anyway.
    """
    return {name: _authorized(agent, name) for name in sorted(allowed_tools(agent))}


def _authorized(agent: str, tool: str) -> Callable:
    implementation = TOOL_IMPLEMENTATIONS[tool]

    def call(**kwargs):
        check_access(agent, tool)
        return implementation(**kwargs)

    call.__name__ = tool
    return call
