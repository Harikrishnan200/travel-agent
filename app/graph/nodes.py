"""The workflow nodes.

Phase 2: every node is a deterministic stub so the graph runs end to end with no
LLM, no MCP and no database. Each stub carries a note naming the phase that
replaces it. The routing logic (validate -> replan -> human review) is real from
day one, because that is the part worth getting right early.
"""

from datetime import date, timedelta

from app.agents import travel_manager
from app.agents.accommodation_agent import research_accommodation as accommodation_agent
from app.agents.experience_agent import research_experience as experience_agent
from app.agents.transport_agent import research_transport as transport_agent
from app.config import get_settings
from app.graph.state import TravelState
from app.tools import get_tools

settings = get_settings()

# Rough local travel allowance per person per day (autos, metro, taxis).
LOCAL_TRANSPORT_PER_DAY = 250


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
    """Delegate to the Transport Agent with a scoped brief."""
    brief = travel_manager.build_transport_brief(state)
    result = transport_agent(brief, get_tools("transport_agent"))

    options = state.get("transport_options", {})
    return {
        "transport_options": {
            **options,
            "alternatives": [o.model_dump() for o in result.alternatives],
        },
        "selected_transport": (
            {**result.recommended.model_dump(), "reason": result.reason}
            if result.recommended
            else {}
        ),
        "degraded": state.get("degraded", []) + result.failures,
    }


def research_accommodation(state: TravelState) -> dict:
    """Delegate to the Accommodation Agent. Its budget depends on the transport chosen."""
    brief = travel_manager.build_accommodation_brief(state)
    result = accommodation_agent(brief, get_tools("accommodation_agent"))

    return {
        "hotel_options": [h.model_dump() for h in result.alternatives],
        "selected_hotel": (
            {**result.recommended.model_dump(), "reason": result.reason}
            if result.recommended
            else {}
        ),
        "degraded": state.get("degraded", []) + result.failures,
    }


def research_experience(state: TravelState) -> dict:
    """Delegate to the Experience Agent: places, weather and a weather-aware day split."""
    brief = travel_manager.build_experience_brief(state)
    result = experience_agent(brief, get_tools("experience_agent"))

    return {
        "places": [p.model_dump() for p in result.places],
        "weather": [w.model_dump() for w in result.weather],
        "routes": [r.model_dump() for r in result.routes],
        "suggested_schedule": result.suggested_schedule,
        "experience_notes": result.notes,
        "degraded": state.get("degraded", []) + result.failures,
    }


# --------------------------------------------------------------------------
# 3. Plan, validate, re-plan
# --------------------------------------------------------------------------


def create_itinerary(state: TravelState) -> dict:
    """Turn the Experience Agent's day split into a morning/afternoon/evening schedule.

    A node, not an agent: it calls no tools. Phase 8 gives it an LLM for the
    narrative wording. The dates and the costs below stay in Python permanently.
    """
    start = date.fromisoformat(state["start_date"])
    schedule = state.get("suggested_schedule") or []
    travelers = state.get("travelers", 1)
    slots = ("morning", "afternoon", "evening")

    days = []
    for index in range(state["duration_days"]):
        day_date = (start + timedelta(days=index)).isoformat()
        planned = next((d for d in schedule if d["date"] == day_date), {})
        activities = planned.get("activities", [])

        day = {
            "day": index + 1,
            "date": day_date,
            "activities": [a["name"] for a in activities],
            "activity_cost": sum(a.get("entry_fee", 0) for a in activities) * travelers,
            "local_transport_cost": LOCAL_TRANSPORT_PER_DAY * travelers,
            "notes": _weather_note(planned.get("weather"), activities),
        }
        for slot, activity in zip(slots, activities, strict=False):
            day[slot] = activity["name"]
        for slot in slots:
            day.setdefault(slot, "Free time")

        # The first and last days are partly spent travelling.
        if index == 0:
            day["morning"] = f"Travel from {state['origin']}, arrival and hotel check-in"
        if index == state["duration_days"] - 1:
            day["evening"] = f"Check out and return to {state['origin']}"

        days.append(day)

    return {"itinerary": {"days": days}}


def _weather_note(weather: dict | None, activities: list[dict]) -> str:
    """A short note explaining the weather, and flagging outdoor plans on a wet day."""
    if not weather:
        return ""
    summary = f"{weather['condition'].title()}, {weather['temperature_c']}C"
    if weather.get("good_for_outdoor"):
        return f"{summary}."
    outdoors = [a["name"] for a in activities if a.get("outdoor")]
    if outdoors:
        return (
            f"{summary} ({weather['rain_probability']}% rain) - {', '.join(outdoors)} is outdoors."
        )
    return f"{summary} ({weather['rain_probability']}% rain) - indoor plans."


def compute_costs(state: TravelState) -> dict:
    """Financial totals, always in Python. Never delegated to the LLM.

    Not a graph node - the validator, the re-planner and the output guard all
    call it, so they can never disagree about the arithmetic.
    """
    travelers = state.get("travelers", 1)
    transport = state.get("selected_transport") or {}
    hotel = state.get("selected_hotel") or {}
    days = state.get("itinerary", {}).get("days", [])

    # Providers return a total for the whole party; fall back to per-person maths.
    one_way = transport.get("total_price") or transport.get("price_per_person", 0) * travelers
    nights = max(state.get("duration_days", 1) - 1, 0)
    stay = hotel.get("total_price") or hotel.get("price_per_night", 0) * nights

    breakdown = {
        "transport": float(one_way) * 2,  # return journey
        "hotel": float(stay),
        "activity": float(sum(d.get("activity_cost", 0) for d in days)),
        "local_transport": float(sum(d.get("local_transport_cost", 0) for d in days)),
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
    """Fix what the validator complained about.

    Phase 9 adds the transport and activity steps; for now the first and
    cheapest lever is the hotel.
    """
    attempts = state.get("replan_attempts", 0) + 1
    updates: dict = {"replan_attempts": attempts}

    current = (state.get("selected_hotel") or {}).get("total_price", 0)
    cheaper = [h for h in state.get("hotel_options", []) if h.get("total_price", 0) < current]
    if cheaper:
        # Best-rated among the cheaper options, not simply the cheapest.
        best = max(cheaper, key=lambda h: (h.get("rating", 0), -h.get("total_price", 0)))
        updates["selected_hotel"] = {
            **best,
            "reason": f"Downgraded to {best['name']} to fit the budget.",
        }
        # Drop it from the pool so the next attempt tries something different.
        updates["hotel_options"] = [h for h in state["hotel_options"] if h["name"] != best["name"]]

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
    return {"status": "approved", "final_response": travel_manager.summarize_for_human(state)}


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
