"""Trip planning endpoints.

Trip state lives in the LangGraph checkpointer, keyed by thread_id (which is the
trip_id). That means there is no second copy of the state to keep in sync - the
Postgres checkpointer in Phase 10 swaps in behind the same two functions.
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.graph.graph import build_graph
from app.graph.state import new_state

router = APIRouter(prefix="/api/v1/trips", tags=["trips"])

# Compiled once: building the graph on every request would recompile it for
# nothing. Phase 10 passes in the Postgres checkpointer here.
_graph = build_graph()


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


def _to_response(trip_id: str, state: dict) -> TripResponse:
    """Shape internal state into the public response. Raw tool payloads stay internal."""
    plan = None
    if state.get("itinerary"):
        plan = {
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
    return TripResponse(
        trip_id=trip_id,
        status=state.get("status", "unknown"),
        plan=plan,
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
    state = _graph.invoke(new_state(trip_id, request.message), config=config)
    return _to_response(trip_id, state)


@router.get("/{trip_id}", response_model=TripResponse)
def get_trip(trip_id: str) -> TripResponse:
    """Fetch the current state of a trip."""
    snapshot = _graph.get_state({"configurable": {"thread_id": trip_id}})
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Trip not found")
    return _to_response(trip_id, snapshot.values)
