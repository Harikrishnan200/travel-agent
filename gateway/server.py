"""MCP Gateway: the only door between the agents and the MCP servers.

    Agents -> Gateway -> transport | hotels | places | weather MCP

It authenticates the caller, checks the agent is allowed the tool it asked for,
rate limits it, routes the call, and logs the outcome. Endpoints are thin: all
the logic is in gateway/service.py.

Run standalone:
    uvicorn gateway.server:app --port 9000
"""

import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app import metrics
from app.config import get_settings
from app.logging_setup import configure_logging
from gateway import service
from gateway.mcp_router import MCPToolError, MCPUnavailableError
from gateway.policy import AGENT_TOOL_ACCESS, ToolNotAllowedError

configure_logging()
logger = logging.getLogger("gateway")
settings = get_settings()

app = FastAPI(title="MCP Gateway", version="0.1.0")


def authenticate(x_api_key: str = Header(default="")) -> None:
    """Shared-secret auth. Enough for a gateway that is not on the public internet."""
    if x_api_key != settings.mcp_gateway_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


def identify_agent(x_agent_name: str = Header(default="")) -> str:
    """Which agent is calling. Authorization is decided from this."""
    if x_agent_name not in AGENT_TOOL_ACCESS:
        raise HTTPException(status_code=403, detail=f"Unknown agent {x_agent_name!r}.")
    return x_agent_name


class ToolCallRequest(BaseModel):
    tool: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    trip_id: str | None = None
    approval_token: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "split_processes": settings.split_processes}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint.

    Metrics live in the process that records them, and tool calls are executed
    here. Without this endpoint, tool_calls_total would be collected in the
    gateway and readable by nobody whenever the two run apart.
    """
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/tools", dependencies=[Depends(authenticate)])
def list_tools(agent: str = Depends(identify_agent)) -> dict:
    """Tool discovery, already filtered to this agent's domain."""
    return {"agent": agent, "tools": service.describe_tools(agent)}


@app.post("/call", dependencies=[Depends(authenticate)])
def call_tool(request: ToolCallRequest, agent: str = Depends(identify_agent)) -> dict:
    """Execute one tool call. Every failure maps to a status code the caller can act on."""
    try:
        result = service.execute_tool(
            agent=agent,
            tool=request.tool,
            arguments=request.arguments,
            approval_token=request.approval_token,
            trip_id=request.trip_id,
        )
    except ToolNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except service.HumanApprovalRequired as exc:
        raise HTTPException(
            status_code=403, detail={"error": "human_approval_required", "message": str(exc)}
        ) from exc
    except service.RateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except MCPUnavailableError as exc:
        # The agent should carry on with whatever else worked.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MCPToolError as exc:
        # The tool ran and rejected the arguments: the caller's fault, not an outage.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"tool": request.tool, "result": result}
