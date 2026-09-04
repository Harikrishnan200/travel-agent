"""Routes a tool call to the MCP server that owns it.

Two transports, chosen by SPLIT_PROCESSES:

- in-process: import the FastMCP server and call it directly. One process, fast
  tests, no ports to manage.
- http: open a streamable-HTTP MCP session to the server's own port. This is
  what docker-compose runs, and it is real MCP over the wire.

The tool schemas, validation and results are identical either way; only the
transport differs.
"""

import asyncio
import json
from typing import Any

from app.config import get_settings

settings = get_settings()

# tool name -> the MCP server that owns it.
TOOL_SERVER = {
    "search_flights": "transport",
    "search_trains": "transport",
    "search_buses": "transport",
    "search_hotels": "hotels",
    "search_places": "places",
    "calculate_route": "places",
    "get_weather": "weather",
}


class MCPUnavailableError(RuntimeError):
    """An MCP server could not be reached or did not answer."""


class MCPToolError(RuntimeError):
    """The MCP server ran the tool and it failed (bad arguments, unknown city...)."""


def server_url(server: str) -> str:
    return {
        "transport": settings.transport_mcp_url,
        "hotels": settings.hotels_mcp_url,
        "places": settings.places_mcp_url,
        "weather": settings.weather_mcp_url,
    }[server]


def call_tool(tool: str, arguments: dict[str, Any]) -> Any:
    """Execute `tool` on whichever MCP server owns it, and return plain Python."""
    server = TOOL_SERVER[tool]
    if settings.split_processes:
        return _run(_call_over_http(server, tool, arguments))
    return _run(_call_in_process(server, tool, arguments))


def _run(coro):
    """Run an async MCP call from synchronous code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("call_tool() is synchronous; call it from a sync context or a thread.")


async def _call_in_process(server: str, tool: str, arguments: dict) -> Any:
    from mcp_servers.hotels.server import mcp as hotels_mcp
    from mcp_servers.places.server import mcp as places_mcp
    from mcp_servers.transport.server import mcp as transport_mcp
    from mcp_servers.weather.server import mcp as weather_mcp

    servers = {
        "transport": transport_mcp,
        "hotels": hotels_mcp,
        "places": places_mcp,
        "weather": weather_mcp,
    }
    try:
        outcome = await servers[server].call_tool(tool, arguments)
    except Exception as exc:  # the tool itself failed
        raise MCPToolError(str(exc)) from exc
    return _unwrap(outcome)


async def _call_over_http(server: str, tool: str, arguments: dict) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = server_url(server)
    try:
        async with streamablehttp_client(url, timeout=settings.mcp_timeout_seconds) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
    except Exception as exc:  # could not reach the server at all
        raise MCPUnavailableError(f"{server} MCP server at {url} is unreachable: {exc}") from exc

    if result.isError:
        raise MCPToolError(result.content[0].text if result.content else "tool failed")
    return _unwrap((result.content, result.structuredContent))


def _unwrap(outcome: Any) -> Any:
    """Normalise the several shapes an MCP result can take into plain Python."""
    if isinstance(outcome, tuple):
        content, structured = outcome
        if isinstance(structured, dict):
            # FastMCP wraps a bare list return value under "result".
            return structured["result"] if set(structured) == {"result"} else structured
        outcome = content
    if outcome and hasattr(outcome[0], "text"):
        return json.loads(outcome[0].text)
    return outcome
