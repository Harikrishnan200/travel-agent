"""The single state object that flows through the LangGraph workflow.

Deliberately flat: every field is a primitive, list or dict so the state
serialises cleanly into a checkpoint (Postgres, from Phase 10).
"""

from typing import TypedDict


class TravelState(TypedDict, total=False):
    # --- identity ---
    trip_id: str
    user_request: str

    # --- extracted requirements (parse_request) ---
    origin: str
    destination: str
    start_date: str  # ISO date, computed in Python
    end_date: str  # ISO date, computed in Python
    duration_days: int
    travelers: int
    budget: float
    preferences: dict  # e.g. {"transport_mode": "train", "max_journey_hours": 12}
    interests: list[str]  # e.g. ["historical", "food"]

    # --- raw research results (kept as an audit trail of what tools returned) ---
    transport_options: dict  # {"flights": [...], "trains": [...], "buses": [...]}
    hotel_options: list
    places: list
    weather: list
    routes: list
    suggested_schedule: list  # Experience Agent's weather-aware day split
    experience_notes: list[str]

    # --- agent recommendations ---
    selected_transport: dict
    selected_hotel: dict

    # --- plan ---
    itinerary: dict  # {"days": [...], "overview": str}
    cost_breakdown: dict  # transport / hotel / activity / local_transport
    total_cost: float

    # --- control flow ---
    status: str  # see STATUSES below
    validation_errors: list[str]
    replan_attempts: int
    degraded: list[str]  # MCP/LLM failures worth telling the user about

    # --- human in the loop ---
    human_feedback: str
    human_approved: bool

    # --- output ---
    final_response: str


# Every status the workflow can leave a trip in. Kept here so the API and the
# nodes agree on the vocabulary.
STATUSES = {
    "planning",  # research/itinerary in progress
    "awaiting_approval",  # valid plan, waiting for the human
    "approved",  # human approved, finalised
    "rejected",  # human rejected
    "needs_user_decision",  # re-planning exhausted, constraints impossible
    "invalid_request",  # input guardrail rejected the request
    "failed",  # unrecoverable error (e.g. all LLMs down)
}


def new_state(trip_id: str, user_request: str) -> TravelState:
    """Create the starting state for a trip."""
    return TravelState(
        trip_id=trip_id,
        user_request=user_request,
        status="planning",
        validation_errors=[],
        replan_attempts=0,
        degraded=[],
        human_approved=False,
    )
