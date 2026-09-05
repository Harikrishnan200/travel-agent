"""The workflow nodes.

Each node takes the state and returns only the keys it changes. The split of
work is deliberate:

- the LLM reads the traveller's message and (from Phase 8) writes the schedule;
- Python owns every date and every rupee, in compute_costs and validate_plan.

Nodes still carrying a stub note name the phase that finishes them.
"""

import logging
from datetime import date, timedelta

from pydantic import BaseModel

from app.agents import travel_manager
from app.agents.accommodation_agent import research_accommodation as accommodation_agent
from app.agents.experience_agent import research_experience as experience_agent
from app.agents.transport_agent import research_transport as transport_agent
from app.config import get_settings
from app.graph import extraction
from app.graph.prompts import DESCRIBE_ITINERARY_SYSTEM, DESCRIBE_ITINERARY_USER
from app.graph.state import TravelState
from app.guardrails import output_guard as output_rules
from app.guardrails.input_guard import InputRejected, check_message, check_requirements
from app.llm.client import LLMUnavailable, call_llm
from app.tools import get_tools
from app.validation import replan as replanner
from app.validation import validators

logger = logging.getLogger(__name__)
settings = get_settings()

# Rough local travel allowance per person per day (autos, metro, taxis).
LOCAL_TRANSPORT_PER_DAY = 250


# --------------------------------------------------------------------------
# 1. Understand the request
# --------------------------------------------------------------------------


def parse_request(state: TravelState) -> dict:
    """Extract structured requirements from the traveller's message.

    The LLM does the reading. If no provider answers we fall back to a plain
    keyword reader so the system still plans a trip with no API keys configured,
    and record that we did - a degraded extraction should never look like a
    normal one.

    Fields already present in the state are never overwritten: a resumed trip
    keeps the requirements extracted on an earlier run.
    """
    message = state.get("user_request", "")
    degraded = list(state.get("degraded", []))

    # Screen the raw text before it reaches a model at all.
    try:
        check_message(message)
    except InputRejected as rejection:
        return {"status": "invalid_request", "validation_errors": [rejection.reply]}

    try:
        extracted, result = extraction.extract_with_llm(message)
        if result.fallback_used:
            degraded.append(f"primary LLM unavailable, used {result.provider}")
    except LLMUnavailable as exc:
        logger.warning("llm unavailable, falling back to rule-based extraction: %s", exc)
        extracted = extraction.extract_with_rules(message)
        degraded.append("no LLM available, requirements were read with simple rules")

    if not extracted.is_travel_request:
        return {
            "status": "invalid_request",
            "validation_errors": [
                "I am a travel planning assistant. Please describe the trip you would "
                "like to plan, including where you are travelling from and to."
            ],
            "degraded": degraded,
        }

    fields = extraction.to_state(extracted)
    updates = {key: value for key, value in fields.items() if state.get(key) in (None, "", 0, [])}
    return {**updates, "degraded": degraded}


def validate_request(state: TravelState) -> dict:
    """Input guardrail on the extracted requirements."""
    # parse_request may already have rejected the message. Re-checking here would
    # bury that answer under field-level noise.
    if state.get("status") == "invalid_request":
        return {}

    errors = check_requirements(state)
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

    # The LLM writes the prose; every date and cost above was computed in Python.
    overview, degraded = _describe_itinerary(state, days)

    return {
        "itinerary": {"days": days, "overview": overview},
        "degraded": _merge_degraded(state, degraded),
    }


def _merge_degraded(state: TravelState, notes: list[str]) -> list[str]:
    """Add notes without repeating ones already recorded."""
    existing = list(state.get("degraded", []))
    return existing + [note for note in notes if note not in existing]


class DayDescription(BaseModel):
    day: int
    summary: str


class ItineraryDescription(BaseModel):
    overview: str
    days: list[DayDescription]


def _describe_itinerary(state: TravelState, days: list[dict]) -> tuple[str, list[str]]:
    """Ask the LLM to narrate the plan. Returns (overview, degraded notes).

    The model is given the finished days and may only describe them. Anything it
    says about a day we did not plan, or a day count that does not match, is
    discarded - it writes prose, it does not get a vote on the plan.
    """
    listing = "\n".join(
        f"Day {day['day']} ({day['date']}): " + (", ".join(day["activities"]) or "no fixed plans")
        for day in days
    )
    try:
        described, result = call_llm(
            DESCRIBE_ITINERARY_SYSTEM,
            DESCRIBE_ITINERARY_USER.format(
                origin=state.get("origin"),
                destination=state.get("destination"),
                duration=state.get("duration_days"),
                travelers=state.get("travelers"),
                interests=", ".join(state.get("interests") or []) or "no particular preference",
                days=listing,
            ),
            ItineraryDescription,
        )
    except LLMUnavailable:
        return "", ["no LLM available, the itinerary has no written summary"]

    by_day = {entry.day: entry.summary for entry in described.days}
    for day in days:
        summary = by_day.get(day["day"])
        if summary:
            day["summary"] = summary

    degraded = []
    if result.fallback_used:
        degraded.append(f"primary LLM unavailable, used {result.provider}")
    if set(by_day) != {day["day"] for day in days}:
        degraded.append("the written summary did not cover every day")

    return described.overview, degraded


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
    """Deterministic validation. Every rule lives in app/validation/validators.py."""
    costs = compute_costs(state)
    errors = validators.validate_plan(state, costs["total_cost"])
    return {**costs, "validation_errors": errors}


def replan(state: TravelState) -> dict:
    """Fix what the validator complained about. The levers live in validation/replan.py."""
    return replanner.replan(state, compute_costs)


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
    """Re-planning could not satisfy the constraints, so hand the decision back.

    The message is built from the errors that actually remain: telling someone to
    raise their budget when a search provider is down would be misleading.
    """
    errors = state.get("validation_errors", [])
    degraded = state.get("degraded", [])

    if any("No transport" in e or "No accommodation" in e for e in errors):
        opening = (
            "I could not put together a complete plan because part of the search "
            "did not return any options."
        )
        ask = "Would you like to try different dates or a different destination?"
    else:
        opening = "Your current requirements do not fit within the budget."
        ask = "Would you like to increase the budget or relax a preference?"

    lines = [opening, "", *(f"- {error}" for error in errors)]
    if degraded:
        lines += ["", "Some searches were unavailable: " + "; ".join(sorted(set(degraded)))]
    lines += ["", ask]

    return {"status": "needs_user_decision", "final_response": "\n".join(lines)}


def finalize(state: TravelState) -> dict:
    """Human approved: assemble the response text."""
    return {"status": "approved", "final_response": travel_manager.summarize_for_human(state)}


def output_guard(state: TravelState) -> dict:
    """Last deterministic check before the response leaves the system.

    The numbers are re-derived rather than trusted, so a total that drifted
    during re-planning is caught here rather than shown to the traveller.
    """
    recomputed = compute_costs(state)
    errors = output_rules.check_plan(state, recomputed["total_cost"])
    if errors:
        logger.error("output guard rejected the plan: %s", errors)
        return {"status": "failed", "validation_errors": errors}
    return {}
