"""The MCP Gateway's actual work: authorize, rate limit, route, log.

gateway/server.py is a thin HTTP wrapper around execute_tool(). Keeping the
logic in a plain function means the same checks run whether the agents call the
gateway over HTTP or in-process, so the two modes cannot drift.
"""

import logging
import time
from collections import defaultdict
from typing import Any

from app.config import get_settings
from gateway import mcp_router
from gateway.policy import ToolNotAllowedError, check_access, requires_human_approval

logger = logging.getLogger("gateway")
settings = get_settings()


class RateLimitExceeded(RuntimeError):
    """The caller made too many tool calls in the current window."""


class HumanApprovalRequired(PermissionError):
    """An action tool was called without an approval token."""


# agent -> list of call timestamps in the current window.
# Phase 6 replaces this with Redis so it survives a restart and works across
# processes. An in-memory counter is enough while there is one gateway.
_calls: dict[str, list[float]] = defaultdict(list)

WINDOW_SECONDS = 60


def check_rate_limit(agent: str) -> None:
    """Allow at most N calls per agent per minute."""
    now = time.monotonic()
    recent = [t for t in _calls[agent] if now - t < WINDOW_SECONDS]
    if len(recent) >= settings.tool_rate_limit_per_minute:
        _calls[agent] = recent
        raise RateLimitExceeded(
            f"Tool rate limit exceeded ({settings.tool_rate_limit_per_minute}/min). "
            "Please try again later."
        )
    recent.append(now)
    _calls[agent] = recent


def reset_rate_limits() -> None:
    """Clear the counters. Used by tests and on gateway startup."""
    _calls.clear()


def execute_tool(
    agent: str,
    tool: str,
    arguments: dict[str, Any],
    approval_token: str | None = None,
    trip_id: str | None = None,
) -> Any:
    """Run one tool call on behalf of one agent, if it is allowed to.

    Raises ToolNotAllowedError, HumanApprovalRequired, RateLimitExceeded,
    MCPUnavailableError or MCPToolError. The caller decides what to do with each.
    """
    started = time.monotonic()

    # 1. Is this agent allowed to call this tool at all?
    check_access(agent, tool)

    # 2. Action tools change the world, so a human must have approved them.
    if requires_human_approval(tool) and not approval_token:
        raise HumanApprovalRequired(f"{tool} requires human approval before it can run.")

    # 3. Is the agent calling too fast?
    check_rate_limit(agent)

    # 4. Route to whichever MCP server owns the tool.
    try:
        result = mcp_router.call_tool(tool, arguments)
    except Exception as exc:
        _log(agent, tool, trip_id, started, "error", type(exc).__name__)
        raise

    _log(agent, tool, trip_id, started, "ok")
    return result


def _log(agent, tool, trip_id, started, status, error=None):
    """One structured line per tool call. Phase 12 swaps this for structlog."""
    duration_ms = (time.monotonic() - started) * 1000
    logger.info(
        "tool_call agent=%s tool=%s trip=%s duration_ms=%.1f status=%s%s",
        agent,
        tool,
        trip_id or "-",
        duration_ms,
        status,
        f" error={error}" if error else "",
    )


def describe_tools(agent: str) -> list[dict]:
    """Tool discovery, filtered to what this agent may call.

    Filtering at discovery matters: an agent's LLM never sees a tool outside its
    domain, so it cannot ask for one. The check in execute_tool is the backstop.
    """
    from gateway.policy import allowed_tools

    return [
        {
            "name": tool,
            "server": mcp_router.TOOL_SERVER.get(tool),
            "requires_approval": requires_human_approval(tool),
        }
        for tool in sorted(allowed_tools(agent))
    ]


__all__ = [
    "HumanApprovalRequired",
    "RateLimitExceeded",
    "ToolNotAllowedError",
    "describe_tools",
    "execute_tool",
    "reset_rate_limits",
]
