"""Trip planning endpoints.

The workflow state lives in the LangGraph checkpointer, keyed by thread id
(which is the trip id). That is the resumable copy. Alongside it we keep a row
in `trips` for the questions the checkpointer cannot answer - listing a user's
trips, or looking one up by status.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import database
from app.graph.graph import build_graph
from app.graph.state import new_state

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


def _to_response(trip_id: str, state: dict) -> TripResponse:
    return TripResponse(
        trip_id=trip_id,
        status=state.get("status", "unknown"),
        plan=_plan_of(state),
        total_cost=state.get("total_cost"),
        validation_errors=state.get("validation_errors", []),
        degraded=state.get("degraded", []),
        final_response=state.get("final_response"),
    )


@router.post("", response_model=TripResponse, status_code=201)
def create_trip(request: CreateTripRequest) -> TripResponse:
    """Start planning a trip. Runs until the plan is ready for human review."""
    trip_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": trip_id}}

    # The row goes in before anything else, so a trip that fails mid-plan still
    # leaves a record, and so its events have something to hang off.
    initial = new_state(trip_id, request.message)
    database.save_trip(trip_id, initial)
    database.record_event(trip_id, "trip_requested", {"message": request.message})

    state = get_graph().invoke(initial, config=config)

    plan = _plan_of(state)
    database.save_trip(trip_id, state, plan)
    database.record_event(
        trip_id,
        "planning_finished",
        {
            "status": state.get("status"),
            "total_cost": state.get("total_cost"),
            "replan_attempts": state.get("replan_attempts"),
            "validation_errors": state.get("validation_errors"),
        },
    )
    return _to_response(trip_id, state)


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
    snapshot = get_graph().get_state({"configurable": {"thread_id": trip_id}})
    if snapshot.values:
        return _to_response(trip_id, snapshot.values)

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
