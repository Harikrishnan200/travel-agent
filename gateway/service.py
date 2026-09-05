"""The MCP Gateway's actual work: authorize, rate limit, route, log.

gateway/server.py is a thin HTTP wrapper around execute_tool(). Keeping the
logic in a plain function means the same checks run whether the agents call the
gateway over HTTP or in-process, so the two modes cannot drift.
"""

import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app import cache, metrics
from app.config import get_settings
from app.logging_setup import get_logger
from gateway import mcp_router
from gateway.policy import (
    READ_TOOLS,
    ToolNotAllowedError,
    check_access,
    requires_human_approval,
)

logger = get_logger("gateway")
settings = get_settings()


class RateLimitExceeded(RuntimeError):
    """The caller made too many tool calls in the current window."""


class HumanApprovalRequired(PermissionError):
    """An action tool was called without an approval token."""


# Which tools were called during the current request, and whether the cache
# answered. A context variable for the same reason the LLM one is: the question
# is "what did this API call do?", and a resumed trip has already done work on
# an earlier call.
_tool_usage: ContextVar[list[dict] | None] = ContextVar("tool_usage", default=None)


@contextmanager
def track_tool_usage():
    """Collect the tool calls made inside this block."""
    collected: list[dict] = []
    token = _tool_usage.set(collected)
    try:
        yield lambda: list(collected)
    finally:
        _tool_usage.reset(token)


def record_tool_call(tool: str, cache_hit: bool, status: str = "ok") -> None:
    """Note one tool call.

    Called from two places: here, when the gateway runs in the same process as
    the agents, and from app/mcp_client.py when it does not - the gateway is
    then a different process and can only report back over HTTP.
    """
    calls = _tool_usage.get()
    if calls is not None:
        calls.append({"tool": tool, "cache_hit": cache_hit, "status": status})


def summarize_tool_usage(calls: list[dict]) -> dict | None:
    """One line about the tools behind a response, for the API to return."""
    if not calls:
        return None

    hits = [call for call in calls if call["cache_hit"]]
    return {
        "calls": len(calls),
        "cache_hits": len(hits),
        "cache_misses": len(calls) - len(hits),
        "failed": sum(1 for call in calls if call["status"] != "ok"),
        "tools": sorted({call["tool"] for call in calls}),
    }


# agent -> list of call timestamps in the current window.
# Phase 6 replaces this with Redis so it survives a restart and works across
# processes. An in-memory counter is enough while there is one gateway.
_calls: dict[str, list[float]] = defaultdict(list)

WINDOW_SECONDS = 60


def check_rate_limit(agent: str) -> None:
    """Allow at most N calls per agent per minute.

    Redis counts the window when it is available, so the limit holds across
    processes and survives a restart. Without Redis we fall back to the local
    counter rather than letting every call through.
    """
    limit = settings.tool_rate_limit_per_minute
    window = int(time.time()) // WINDOW_SECONDS
    count = cache.increment_window(f"ratelimit:{agent}:{window}", WINDOW_SECONDS)

    if count is None:
        count = _count_locally(agent)

    if count > limit:
        raise RateLimitExceeded(f"Tool rate limit exceeded ({limit}/min). Please try again later.")


def _count_locally(agent: str) -> int:
    """In-memory sliding window, used only when Redis is unreachable."""
    now = time.monotonic()
    recent = [t for t in _calls[agent] if now - t < WINDOW_SECONDS]
    recent.append(now)
    _calls[agent] = recent
    return len(recent)


def reset_rate_limits() -> None:
    """Clear the counters. Used by tests and on gateway startup."""
    _calls.clear()
    client = cache.get_client()
    if client is not None:
        try:
            for key in client.scan_iter("ratelimit:*"):
                client.delete(key)
        except Exception:
            pass


def was_cache_hit(tool: str, arguments: dict[str, Any]) -> bool:
    """Whether this exact call is already in the cache.

    Used by the HTTP endpoint to tell the caller how their result was produced.
    The lookup is cheap and the value is about to be fetched anyway.
    """
    from gateway.policy import READ_TOOLS

    if tool not in READ_TOOLS:
        return False
    return cache.get(cache.make_key(tool, arguments)) is not None


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

    # 3. Serve read tools from cache when we can. Action tools are never cached.
    key = cache.make_key(tool, arguments) if tool in READ_TOOLS else None
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            record_tool_call(tool, cache_hit=True)
            _log(agent, tool, trip_id, started, "ok", cache_hit=True)
            return hit

    # 4. Is the agent calling too fast? Checked after the cache, because the
    # limit exists to protect the MCP servers and a cache hit never reaches one.
    check_rate_limit(agent)

    # 5. Route to whichever MCP server owns the tool.
    try:
        result = mcp_router.call_tool(tool, arguments)
    except Exception as exc:
        record_tool_call(tool, cache_hit=False, status="error")
        _log(agent, tool, trip_id, started, "error", type(exc).__name__)
        raise

    if key is not None:
        cache.set(key, result, cache.ttl_for(tool))

    record_tool_call(tool, cache_hit=False)
    _log(agent, tool, trip_id, started, "ok")
    return result


def _log(agent, tool, trip_id, started, status, error=None, cache_hit=False):
    """One structured line and one metric per tool call."""
    duration_ms = (time.monotonic() - started) * 1000
    metrics.tool_calls.labels(tool=tool, status=status, cache_hit=str(cache_hit).lower()).inc()
    logger.info(
        "tool_call",
        agent=agent,
        tool=tool,
        trip_id=trip_id,
        duration_ms=round(duration_ms, 1),
        status=status,
        cache_hit=cache_hit,
        error=error,
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
    "record_tool_call",
    "reset_rate_limits",
    "summarize_tool_usage",
    "track_tool_usage",
]
