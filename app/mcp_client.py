"""How the agents reach the MCP Gateway.

Two modes, chosen by SPLIT_PROCESSES:

- in-process: call gateway.service.execute_tool() directly. Same authorization,
  rate limiting and routing; no network, no auth header (there is nothing to
  authenticate across).
- http: POST to the gateway with the API key and the agent's name.

Errors are normalised to one exception type so the agents handle failures the
same way in both modes.
"""

import logging
from typing import Any

import httpx

from app.config import get_settings
from gateway import service
from gateway.mcp_router import MCPToolError, MCPUnavailableError
from gateway.policy import ToolNotAllowedError

logger = logging.getLogger(__name__)
settings = get_settings()


class ToolCallFailed(RuntimeError):
    """A tool call did not produce a result. The agent degrades rather than crashes."""


def call_tool(
    agent: str,
    tool: str,
    arguments: dict[str, Any],
    trip_id: str | None = None,
) -> Any:
    """Call one tool through the gateway on behalf of one agent."""
    if settings.split_processes:
        return _call_over_http(agent, tool, arguments, trip_id)
    return _call_in_process(agent, tool, arguments, trip_id)


def _call_in_process(agent: str, tool: str, arguments: dict, trip_id: str | None) -> Any:
    try:
        return service.execute_tool(agent, tool, arguments, trip_id=trip_id)
    except ToolNotAllowedError:
        # A bug in our own wiring, not a runtime hiccup: let it surface loudly.
        raise
    except (service.RateLimitExceeded, MCPUnavailableError, MCPToolError) as exc:
        raise ToolCallFailed(str(exc)) from exc


def _call_over_http(agent: str, tool: str, arguments: dict, trip_id: str | None) -> Any:
    payload = {"tool": tool, "arguments": arguments, "trip_id": trip_id}
    headers = {"X-API-Key": settings.mcp_gateway_api_key, "X-Agent-Name": agent}
    try:
        response = httpx.post(
            f"{settings.mcp_gateway_url}/call",
            json=payload,
            headers=headers,
            timeout=settings.mcp_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise ToolCallFailed(f"MCP gateway is unreachable: {exc}") from exc

    if response.status_code == 403:
        raise ToolNotAllowedError(_detail(response))
    if response.status_code >= 400:
        raise ToolCallFailed(f"gateway returned {response.status_code}: {_detail(response)}")

    return response.json()["result"]


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        return response.text
    return detail if isinstance(detail, str) else str(detail)
