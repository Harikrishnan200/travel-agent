"""Tool dispatch for the agents.

An agent is handed a `{tool_name: callable}` mapping containing only the tools
its domain allows. Every callable goes through the MCP Gateway, which is where
authorization, rate limiting, routing and logging happen.

The mapping is why the agents never changed when tools moved from local function
calls to MCP: they only ever see callables.
"""

from collections.abc import Callable

from app.mcp_client import call_tool
from gateway.policy import allowed_tools


def get_tools(agent: str, trip_id: str | None = None) -> dict[str, Callable]:
    """The tools `agent` may call, each routed through the gateway.

    Handing back a filtered mapping means an agent never sees a tool outside its
    domain. The gateway's own check is the backstop if one is called anyway.
    """
    return {name: _gateway_call(agent, name, trip_id) for name in sorted(allowed_tools(agent))}


def _gateway_call(agent: str, tool: str, trip_id: str | None) -> Callable:
    def call(**arguments):
        return call_tool(agent, tool, arguments, trip_id=trip_id)

    call.__name__ = tool
    return call
