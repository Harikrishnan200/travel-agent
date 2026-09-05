"""Re-planning: fixing what the validator complained about.

Four levers, tried in a fixed order, cheapest change first:

1. a cheaper hotel
2. a cheaper transport option that still respects a stated preference
3. drop or replace the most expensive paid activities
4. move an outdoor activity off a wet day onto a dry one

Each attempt applies the first lever that actually changes something, and the
option it used is removed from the pool, so the next attempt moves on. That is
the whole algorithm - there is no optimiser here, and there does not need to be:
the validator has the final say either way.
"""

import logging

from app.itinerary_rules import day_capacity, is_travel_day

logger = logging.getLogger(__name__)


def replan(state: dict, compute_costs) -> dict:
    """Return the state changes for one re-planning attempt.

    `compute_costs` is passed in rather than imported so this module never has
    to know about the workflow nodes.
    """
    errors = state.get("validation_errors", [])
    updates: dict = {"replan_attempts": state.get("replan_attempts", 0) + 1}

    if _has(errors, "Budget exceeded"):
        for lever in (cheaper_hotel, cheaper_transport, cheaper_activities):
            change = lever(state)
            if change:
                logger.info("replan applied %s", lever.__name__.strip("_"))
                updates.update(change)
                break

    if _has(errors, "rain"):
        change = _reschedule_around_weather(state)
        if change:
            logger.info("replan rescheduled activities around the weather")
            updates.update(change)

    return updates


def _has(errors: list[str], fragment: str) -> bool:
    return any(fragment in error for error in errors)


# --- lever 1: a cheaper hotel ----------------------------------------------


def cheaper_hotel(state: dict) -> dict | None:
    current = (state.get("selected_hotel") or {}).get("total_price", 0)
    cheaper = [h for h in state.get("hotel_options", []) if h.get("total_price", 0) < current]
    if not cheaper:
        return None

    # Best rated among the cheaper options, not simply the cheapest.
    best = max(cheaper, key=lambda h: (h.get("rating", 0), -h.get("total_price", 0)))
    return {
        "selected_hotel": {**best, "reason": f"Moved to {best['name']} to fit the budget."},
        "hotel_options": [h for h in state["hotel_options"] if h["name"] != best["name"]],
    }


# --- lever 2: cheaper transport --------------------------------------------


def cheaper_transport(state: dict) -> dict | None:
    current = state.get("selected_transport") or {}
    alternatives = (state.get("transport_options") or {}).get("alternatives", [])
    cheaper = [
        option
        for option in alternatives
        if option.get("price_per_person", 0) < current.get("price_per_person", 0)
    ]
    if not cheaper:
        return None

    limit = (state.get("preferences") or {}).get("max_journey_hours")
    within_limit = [o for o in cheaper if limit is None or o.get("duration_hours", 0) <= limit]

    # Prefer something that still honours the traveller's time limit. Only if
    # nothing cheaper fits do we offer a slower option, and say so.
    if within_limit:
        choice = min(within_limit, key=lambda o: o["price_per_person"])
        reason = f"Switched to a cheaper {choice['mode']} to fit the budget."
    else:
        choice = min(cheaper, key=lambda o: o["price_per_person"])
        reason = (
            f"Switched to a cheaper {choice['mode']} to fit the budget. "
            f"It takes {choice.get('duration_hours')}h, over the {limit}h you asked for."
        )

    remaining = [o for o in alternatives if o.get("id") != choice.get("id")]
    return {
        "selected_transport": {**choice, "reason": reason},
        "transport_options": {**(state.get("transport_options") or {}), "alternatives": remaining},
    }


# --- lever 3: cheaper activities -------------------------------------------


def cheaper_activities(state: dict) -> dict | None:
    """Swap the priciest paid activity for a free one, or drop it."""
    days = [dict(day) for day in (state.get("itinerary") or {}).get("days", [])]
    if not days:
        return None

    scheduled = {name for day in days for name in day.get("activities", [])}
    free_places = [
        place
        for place in state.get("places", [])
        if not place.get("entry_fee") and place["name"] not in scheduled
    ]

    target = max(days, key=lambda day: day.get("activity_cost", 0))
    if not target.get("activity_cost"):
        return None

    by_name = {place["name"]: place for place in state.get("places", [])}
    priciest = max(
        target.get("activities", []),
        key=lambda name: by_name.get(name, {}).get("entry_fee", 0),
        default=None,
    )
    if priciest is None:
        return None

    travelers = state.get("travelers", 1)
    fee = by_name.get(priciest, {}).get("entry_fee", 0)
    activities = [name for name in target["activities"] if name != priciest]

    if free_places:
        replacement = free_places[0]
        activities.append(replacement["name"])
        note = f"Swapped {priciest} for {replacement['name']}, which is free."
    elif activities:
        note = f"Dropped {priciest} to fit the budget."
    else:
        # Dropping the last activity of a day would leave an empty schedule,
        # which the validator rejects anyway. This lever is spent.
        return None

    target["activities"] = activities
    target["activity_cost"] = max(target.get("activity_cost", 0) - fee * travelers, 0)
    target["notes"] = " ".join(filter(None, [target.get("notes", ""), note]))
    _refill_slots(target)

    return {"itinerary": {**state["itinerary"], "days": days}}


# --- lever 4: the weather ---------------------------------------------------


def _reschedule_around_weather(state: dict) -> dict | None:
    """Trade outdoor activities on wet days for indoor ones on dry days.

    Every conflict that can be resolved is resolved in this one pass. Fixing a
    single conflict per attempt would never converge: a five day trip can easily
    have four rainy days and only three re-planning attempts.

    Two rules keep this from making the plan worse:
    - a dry day is only a candidate if it has room under its own capacity, so
      the arrival day does not end up carrying the whole trip;
    - a wet day is never emptied. An activity moves out only when something
      comes back, or when the day still has other plans.
    """
    days = [dict(day) for day in (state.get("itinerary") or {}).get("days", [])]
    forecast = {entry["date"]: entry for entry in state.get("weather", [])}
    outdoor = {place["name"] for place in state.get("places", []) if place.get("outdoor")}
    if not days or not forecast:
        return None

    swaps = 0
    # Bounded by the number of activities, so this always terminates.
    for _ in range(sum(len(day.get("activities", [])) for day in days) + 1):
        if not _swap_one(days, forecast, outdoor):
            break
        swaps += 1

    if not swaps:
        return None

    for day in days:
        _refill_slots(day)
    return {"itinerary": {**state["itinerary"], "days": days}}


def _swap_one(days: list[dict], forecast: dict, outdoor: set[str]) -> bool:
    """Move one outdoor activity off a wet day. Returns whether anything moved."""
    total = len(days)
    wet_days = [
        day
        for day in days
        if not forecast.get(day["date"], {}).get("good_for_outdoor", True)
        and any(name in outdoor for name in day.get("activities", []))
    ]
    dry_days = sorted(
        (day for day in days if forecast.get(day["date"], {}).get("good_for_outdoor")),
        key=lambda day: forecast[day["date"]]["rain_probability"],
    )

    for wet in wet_days:
        moving = next(name for name in wet["activities"] if name in outdoor)
        for dry in dry_days:
            if dry is wet:
                continue

            swap_back = next((n for n in dry["activities"] if n not in outdoor), None)
            room = len(dry["activities"]) < day_capacity(dry["day"], total)

            # A straight move needs room on the dry day, and must not leave a
            # full day empty. Arrival and departure days may be emptied: they
            # are spent travelling, and the validator allows them to be quiet.
            may_empty = is_travel_day(wet["day"], total) or len(wet["activities"]) > 1

            # A swap needs neither, because the counts stay the same.
            if swap_back is None and not (room and may_empty):
                continue

            wet["activities"] = [n for n in wet["activities"] if n != moving]
            dry["activities"] = [*dry["activities"], moving]
            if swap_back:
                dry["activities"].remove(swap_back)
                wet["activities"].append(swap_back)

            note = f"Moved {moving} to day {dry['day']} for better weather."
            wet["notes"] = " ".join(filter(None, [wet.get("notes", ""), note]))
            return True

    return False


def _refill_slots(day: dict) -> None:
    """Keep morning/afternoon/evening in step with the activity list."""
    slots = ("morning", "afternoon", "evening")
    protected = {
        slot: day[slot]
        for slot in slots
        if isinstance(day.get(slot), str)
        and ("Travel from" in day[slot] or "Check out" in day[slot])
    }

    available = list(day.get("activities", []))
    for slot in slots:
        if slot in protected:
            continue
        day[slot] = available.pop(0) if available else "Free time"

    day.update(protected)
