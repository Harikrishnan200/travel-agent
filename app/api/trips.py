"""Trip planning endpoints.

The workflow state lives in the LangGraph checkpointer, keyed by thread id
(which is the trip id). That is the resumable copy. Alongside it we keep a row
in `trips` for the questions the checkpointer cannot answer - listing a user's
trips, or looking one up by status.
"""

import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.db import database
from app.graph.graph import build_graph
from app.graph.state import new_state
from app.llm.client import summarize_usage, track_usage
from app.logging_setup import get_logger, log_context
from gateway.service import summarize_tool_usage, track_tool_usage

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/trips", tags=["trips"])

_graph = None


def get_graph():
    """Compile the workflow once, on first use, so the checkpointer is ready."""
    global _graph
    if _graph is None:
        from app.db.checkpointer import get_checkpointer

        _graph = build_graph(get_checkpointer())
    return _graph


def reset_graph() -> None:
    """Force the graph to be rebuilt. Used by tests that swap the checkpointer."""
    global _graph
    _graph = None


class CreateTripRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class TripResponse(BaseModel):
    trip_id: str
    status: str
    plan: dict | None = None
    total_cost: float | None = None
    validation_errors: list[str] = []
    degraded: list[str] = []
    final_response: str | None = None
    # Present while the workflow is paused for the traveller.
    awaiting_decision: dict | None = None

    # How long this request took, which LLM answered it, and how much of the
    # work the cache did. Each is null when nothing of that kind happened.
    duration_ms: float | None = None
    llm: dict | None = None
    tools: dict | None = None


class ApproveRequest(BaseModel):
    approved: bool = True


class ModifyRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class TripSummary(BaseModel):
    trip_id: str
    status: str
    origin: str | None = None
    destination: str | None = None
    total_cost: float | None = None


def _plan_of(state: dict) -> dict | None:
    """The public shape of a plan. Raw tool payloads stay internal."""
    if not state.get("itinerary"):
        return None
    return {
        "origin": state.get("origin"),
        "destination": state.get("destination"),
        "start_date": state.get("start_date"),
        "end_date": state.get("end_date"),
        "travelers": state.get("travelers"),
        "budget": state.get("budget"),
        "transport": state.get("selected_transport"),
        "hotel": state.get("selected_hotel"),
        "itinerary": state.get("itinerary"),
        "cost_breakdown": state.get("cost_breakdown"),
    }


def _to_response(
    trip_id: str,
    state: dict,
    decision: dict | None = None,
    duration_ms: float | None = None,
    llm: dict | None = None,
    tools: dict | None = None,
) -> TripResponse:
    return TripResponse(
        trip_id=trip_id,
        status=state.get("status", "unknown"),
        plan=_plan_of(state),
        total_cost=state.get("total_cost"),
        validation_errors=state.get("validation_errors", []),
        degraded=state.get("degraded", []),
        final_response=state.get("final_response"),
        awaiting_decision=decision,
        duration_ms=duration_ms,
        llm=llm,
        tools=tools,
    )


def _elapsed_ms(started: float) -> float:
    """How long a request took, to a tenth of a millisecond."""
    return round((time.monotonic() - started) * 1000, 1)


def _pending_decision(trip_id: str) -> dict | None:
    """What the workflow is waiting for, if it is paused at the interrupt."""
    snapshot = get_graph().get_state({"configurable": {"thread_id": trip_id}})
    for task in snapshot.tasks:
        for pending in task.interrupts:
            return pending.value
    return None


def _resume(trip_id: str, decision: dict, event: str) -> TripResponse:
    """Answer the interrupt and let the workflow carry on."""
    config = {"configurable": {"thread_id": trip_id}}
    snapshot = get_graph().get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Trip not found")
    pending = _pending_decision(trip_id)
    if pending is None:
        raise HTTPException(
            status_code=409,
            detail=f"This trip is not waiting for a decision (status: "
            f"{snapshot.values.get('status')}).",
        )

    offered = pending.get("options", [])
    if decision["action"] not in offered:
        raise HTTPException(
            status_code=409,
            detail=f"This plan cannot be {decision['action']}d. You can: {', '.join(offered)}.",
        )

    started = time.monotonic()
    with (
        log_context(trip_id=trip_id),
        track_usage() as llm_usage,
        track_tool_usage() as tool_usage,
    ):
        database.record_event(trip_id, event, decision)
        logger.info("human_decision", action=decision["action"])

        state = get_graph().invoke(Command(resume=decision), config=config)

        plan = _plan_of(state)
        database.save_trip(trip_id, state, plan)
        llm = summarize_usage(llm_usage())
        tools = summarize_tool_usage(tool_usage())
        database.record_event(
            trip_id, "workflow_continued", {"status": state.get("status"), "tools": tools}
        )
        logger.info("workflow_continued", status=state.get("status"), llm=llm, tools=tools)
        return _to_response(
            trip_id, state, _pending_decision(trip_id), _elapsed_ms(started), llm, tools
        )


@router.post("", response_model=TripResponse, status_code=201)
def create_trip(request: CreateTripRequest) -> TripResponse:
    """Start planning a trip. Runs until the plan is ready for human review."""
    trip_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": trip_id}}

    # The row goes in before anything else, so a trip that fails mid-plan still
    # leaves a record, and so its events have something to hang off.
    started = time.monotonic()
    with (
        log_context(trip_id=trip_id),
        track_usage() as llm_usage,
        track_tool_usage() as tool_usage,
    ):
        initial = new_state(trip_id, request.message)
        database.save_trip(trip_id, initial)
        database.record_event(trip_id, "trip_requested", {"message": request.message})
        logger.info("trip_requested")

        state = get_graph().invoke(initial, config=config)

        plan = _plan_of(state)
        database.save_trip(trip_id, state, plan)
        llm = summarize_usage(llm_usage())
        tools = summarize_tool_usage(tool_usage())
        outcome = {
            "status": state.get("status"),
            "total_cost": state.get("total_cost"),
            "replan_attempts": state.get("replan_attempts"),
            "validation_errors": state.get("validation_errors"),
        }
        database.record_event(trip_id, "planning_finished", {**outcome, "llm": llm, "tools": tools})
        logger.info("planning_finished", **outcome, llm=llm, tools=tools)
        return _to_response(
            trip_id, state, _pending_decision(trip_id), _elapsed_ms(started), llm, tools
        )


@router.post("/{trip_id}/approve", response_model=TripResponse)
def approve_trip(trip_id: str, request: ApproveRequest) -> TripResponse:
    """Approve the plan, or reject it outright."""
    action = "approve" if request.approved else "reject"
    return _resume(trip_id, {"action": action}, f"human_{action}d")


@router.post("/{trip_id}/modify", response_model=TripResponse)
def modify_trip(trip_id: str, request: ModifyRequest) -> TripResponse:
    """Ask for a change. The plan is re-planned and comes back for review."""
    return _resume(
        trip_id, {"action": "modify", "feedback": request.feedback}, "human_requested_change"
    )


@router.post("/{trip_id}/message", response_model=TripResponse)
def send_message(trip_id: str, request: MessageRequest) -> TripResponse:
    """Continue the conversation.

    A message while the plan is waiting for review is treated as a request to
    change it, which is what people actually mean when they reply to a plan.
    """
    return _resume(
        trip_id, {"action": "modify", "feedback": request.message}, "human_requested_change"
    )


@router.get("", response_model=list[TripSummary])
def list_trips(limit: int = Query(default=20, ge=1, le=100)) -> list[TripSummary]:
    """Recent trips. Answered from Postgres, not from the checkpointer."""
    return [
        TripSummary(
            trip_id=trip.trip_id,
            status=trip.status,
            origin=trip.origin,
            destination=trip.destination,
            total_cost=float(trip.total_cost) if trip.total_cost is not None else None,
        )
        for trip in database.list_trips(limit)
    ]


@router.get("/{trip_id}", response_model=TripResponse)
def get_trip(trip_id: str) -> TripResponse:
    """Fetch a trip, resuming it from the checkpointer."""
    started = time.monotonic()
    snapshot = get_graph().get_state({"configurable": {"thread_id": trip_id}})
    if snapshot.values:
        return _to_response(
            trip_id, snapshot.values, _pending_decision(trip_id), _elapsed_ms(started)
        )

    # The checkpoint may be gone (in-memory saver, restarted process) while the
    # trip row survives. Answer from what we still have rather than 404ing.
    stored = database.get_trip(trip_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return TripResponse(
        trip_id=stored.trip_id,
        status=stored.status,
        plan=stored.plan,
        total_cost=float(stored.total_cost) if stored.total_cost is not None else None,
    )


@router.get("/{trip_id}/events")
def get_trip_events(trip_id: str) -> list[dict]:
    """The audit trail for a trip."""
    return [
        {
            "event_type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        }
        for event in database.get_events(trip_id)
    ]
