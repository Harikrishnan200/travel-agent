"""The workflow nodes.

Phase 2: every node is a deterministic stub so the graph runs end to end with no
LLM, no MCP and no database. Each stub carries a note naming the phase that
replaces it. The routing logic (validate -> replan -> human review) is real from
day one, because that is the part worth getting right early.
"""

from datetime import date, timedelta

from app.config import get_settings
from app.graph.state import TravelState

settings = get_settings()


# --------------------------------------------------------------------------
# 1. Understand the request
# --------------------------------------------------------------------------


def parse_request(state: TravelState) -> dict:
    """Extract structured requirements from the user's message.

    Phase 7 replaces this with an LLM structured-output call. The stub returns a
    fixed request so the rest of the graph has something to work with.

    Fields already present in the state are never overwritten: the caller may
    have supplied requirements directly, and later phases resume a trip whose
    requirements were extracted on an earlier run.
    """
    start = date.today() + timedelta(days=14)
    duration = 5
    extracted = {
        "origin": "Kochi",
        "destination": "Delhi",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=duration - 1)).isoformat(),
        "duration_days": duration,
        "travelers": 2,
        "budget": 50000.0,
        "preferences": {"transport_mode": "train", "max_journey_hours": 12},
        "interests": ["historical", "food"],
    }
    return {key: value for key, value in extracted.items() if state.get(key) in (None, "", 0)}


def validate_request(state: TravelState) -> dict:
    """Input guardrail: is this a travel request we can actually plan?

    Phase 8 replaces this with the real guardrail module.
    """
    errors: list[str] = []
    if not state.get("origin"):
        errors.append("Origin city is missing.")
    if not state.get("destination"):
        errors.append("Destination city is missing.")
    if errors:
        return {"status": "invalid_request", "validation_errors": errors}
    return {"status": "planning", "validation_errors": []}


# --------------------------------------------------------------------------
# 2. Research (delegated to the three specialised agents)
# --------------------------------------------------------------------------


def research_transport(state: TravelState) -> dict:
    """Transport Agent: flights vs trains vs buses.

    Phase 3 replaces this with the real agent, Phase 4/5 with MCP tool calls.
    """
    return {
        "transport_options": {
            "trains": [
                {
                    "id": "12626",
                    "name": "Kerala Express",
                    "duration_hours": 46,
                    "price_per_person": 1800,
                }
            ],
            "flights": [
                {
                    "id": "6E-455",
                    "name": "IndiGo",
                    "duration_hours": 3,
                    "price_per_person": 6500,
                }
            ],
            "buses": [],
        },
        "selected_transport": {
            "mode": "flight",
            "id": "6E-455",
            "name": "IndiGo",
            "duration_hours": 3,
            "price_per_person": 6500,
            "reason": "Train journey is 46h, over the traveller's 12h limit.",
        },
    }


def research_accommodation(state: TravelState) -> dict:
    """Accommodation Agent. Phase 3 replaces this with the real agent."""
    return {
        "hotel_options": [
            {"name": "Hotel Sunrise", "rating": 4.2, "price_per_night": 3000},
            {"name": "Budget Inn", "rating": 3.8, "price_per_night": 1800},
        ],
        "selected_hotel": {
            "name": "Hotel Sunrise",
            "location": "Connaught Place, Delhi",
            "rating": 4.2,
            "price_per_night": 3000,
        },
    }


def research_experience(state: TravelState) -> dict:
    """Experience Agent: places, weather, routes. Phase 3 replaces this."""
    return {
        "places": [
            {"name": "Red Fort", "category": "historical", "entry_fee": 600},
            {"name": "Humayun's Tomb", "category": "historical", "entry_fee": 600},
            {"name": "Chandni Chowk food walk", "category": "food", "entry_fee": 0},
        ],
        "weather": [{"date": state.get("start_date", ""), "condition": "clear", "rain_chance": 10}],
        "routes": [],
    }


# --------------------------------------------------------------------------
# 3. Plan, validate, re-plan
# --------------------------------------------------------------------------


def create_itinerary(state: TravelState) -> dict:
    """Turn research results into a day-by-day schedule.

    A node, not an agent: it calls no tools. Phase 8 gives it an LLM for the
    narrative; the costs below stay in Python permanently.
    """
    start = date.fromisoformat(state["start_date"])
    days = []
    for i in range(state["duration_days"]):
        days.append(
            {
                "day": i + 1,
                "date": (start + timedelta(days=i)).isoformat(),
                "morning": "Sightseeing",
                "afternoon": "Local exploration",
                "evening": "Food walk",
                "activities": [p["name"] for p in state.get("places", [])[:2]],
                "activity_cost": 1200,
                "local_transport_cost": 500,
                "notes": "",
            }
        )
    return {"itinerary": {"days": days}}


def compute_costs(state: TravelState) -> dict:
    """Financial totals, always in Python. Never delegated to the LLM.

    Not a graph node - called by validate_plan and by the re-planner so both
    always agree on the arithmetic.
    """
    travelers = state.get("travelers", 1)
    nights = max(state.get("duration_days", 1) - 1, 0)

    transport = state.get("selected_transport", {}).get("price_per_person", 0) * travelers * 2
    hotel = state.get("selected_hotel", {}).get("price_per_night", 0) * nights
    days = state.get("itinerary", {}).get("days", [])
    activity = sum(d.get("activity_cost", 0) for d in days)
    local = sum(d.get("local_transport_cost", 0) for d in days)

    breakdown = {
        "transport": float(transport),
        "hotel": float(hotel),
        "activity": float(activity),
        "local_transport": float(local),
    }
    return {"cost_breakdown": breakdown, "total_cost": float(sum(breakdown.values()))}


def validate_plan(state: TravelState) -> dict:
    """Deterministic validation. Phase 8 moves the rules into validation/."""
    costs = compute_costs(state)
    errors: list[str] = []

    days = state.get("itinerary", {}).get("days", [])
    if len(days) != state.get("duration_days", 0):
        errors.append(f"Itinerary has {len(days)} days, expected {state.get('duration_days')}.")

    budget = state.get("budget", 0)
    if costs["total_cost"] > budget:
        over = costs["total_cost"] - budget
        errors.append(f"Budget exceeded by Rs {over:,.0f}.")

    return {**costs, "validation_errors": errors}


def replan(state: TravelState) -> dict:
    """Fix the errors the validator found. Phase 9 implements the real heuristics."""
    attempts = state.get("replan_attempts", 0) + 1
    updates: dict = {"replan_attempts": attempts}

    # Cheapest available fix: downgrade the hotel.
    cheaper = sorted(state.get("hotel_options", []), key=lambda h: h.get("price_per_night", 0))
    current = state.get("selected_hotel", {}).get("price_per_night", 0)
    for hotel in cheaper:
        if hotel.get("price_per_night", 0) < current:
            updates["selected_hotel"] = {**state["selected_hotel"], **hotel}
            break

    return updates


# --------------------------------------------------------------------------
# 4. Human in the loop, finalise
# --------------------------------------------------------------------------


def human_review(state: TravelState) -> dict:
    """Pause for human approval.

    Phase 11 replaces the body with a LangGraph interrupt(). For now the graph
    simply stops here and the API reports the plan as awaiting approval.
    """
    return {"status": "awaiting_approval"}


def needs_user_decision(state: TravelState) -> dict:
    """Re-planning could not satisfy the constraints - ask the human to relax one."""
    return {
        "status": "needs_user_decision",
        "final_response": (
            "Your requirements do not fit within the budget. "
            "Would you like to increase the budget or relax a preference?\n- "
            + "\n- ".join(state.get("validation_errors", []))
        ),
    }


def finalize(state: TravelState) -> dict:
    """Human approved: assemble the response text."""
    transport = state.get("selected_transport", {})
    hotel = state.get("selected_hotel", {})
    lines = [
        f"{state['origin']} -> {state['destination']}",
        f"Travellers: {state['travelers']}  Duration: {state['duration_days']} days",
        f"Transport: {transport.get('mode')} {transport.get('name')} "
        f"(Rs {transport.get('price_per_person', 0):,}/person)",
        f"Hotel: {hotel.get('name')} (Rs {hotel.get('price_per_night', 0):,}/night)",
        f"Estimated total: Rs {state.get('total_cost', 0):,.0f} "
        f"of a Rs {state.get('budget', 0):,.0f} budget",
    ]
    return {"status": "approved", "final_response": "\n".join(lines)}


def output_guard(state: TravelState) -> dict:
    """Last deterministic check before the response leaves the system.

    Phase 8 replaces this with the real output guardrail.
    """
    recomputed = compute_costs(state)
    if abs(recomputed["total_cost"] - state.get("total_cost", 0)) > 0.01:
        return {
            "status": "failed",
            "validation_errors": ["Final cost did not match the recomputed total."],
        }
    return {}
