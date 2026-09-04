"""Which agent may call which tool.

This is the single source of truth for tool authorization. Phase 5's MCP Gateway
enforces it over HTTP; until then app/tools.py enforces the same table in
process. Defining it once means the local and gateway paths cannot drift.
"""

# Tools that only read data. Safe to call without asking anyone.
READ_TOOLS = {
    "search_flights",
    "search_trains",
    "search_buses",
    "search_hotels",
    "search_places",
    "get_weather",
    "calculate_route",
}

# Tools that would change the world. Never callable without human approval.
# Not implemented as real bookings - they exist to prove the authorization path.
ACTION_TOOLS = {
    "book_flight",
    "book_train",
    "book_bus",
    "book_hotel",
}

ALL_TOOLS = READ_TOOLS | ACTION_TOOLS

# The heart of the multi-agent design: each agent sees a small tool space.
AGENT_TOOL_ACCESS: dict[str, set[str]] = {
    "travel_manager": set(),  # coordinates only, delegates all research
    "transport_agent": {"search_flights", "search_trains", "search_buses"},
    "accommodation_agent": {"search_hotels"},
    "experience_agent": {"search_places", "get_weather", "calculate_route"},
}


class ToolNotAllowedError(PermissionError):
    """An agent asked for a tool outside its allowed set."""


def allowed_tools(agent: str) -> set[str]:
    """Tools this agent may call. Unknown agent -> nothing."""
    return AGENT_TOOL_ACCESS.get(agent, set())


def check_access(agent: str, tool: str) -> None:
    """Raise unless `agent` may call `tool`. Called before every tool execution."""
    if agent not in AGENT_TOOL_ACCESS:
        raise ToolNotAllowedError(f"Unknown agent {agent!r}.")
    if tool not in ALL_TOOLS:
        raise ToolNotAllowedError(f"Unknown tool {tool!r}.")
    if tool not in AGENT_TOOL_ACCESS[agent]:
        raise ToolNotAllowedError(f"Agent {agent!r} is not allowed to call {tool!r}.")


def requires_human_approval(tool: str) -> bool:
    """Action tools need a human. Read tools do not."""
    return tool in ACTION_TOOLS
