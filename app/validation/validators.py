"""Deterministic plan validation.

Every function here is plain Python: same input, same answer, no LLM. They each
return a list of human-readable errors, empty when the plan is fine.

This is the half of the split that matters most: the LLM reasons and writes
prose, Python decides whether a plan is actually valid. Nothing in this file
asks a model anything.
"""

from datetime import date, timedelta

from app.itinerary_rules import day_capacity

# A day with nothing at all planned is a gap, not an itinerary.
MIN_ACTIVITIES_ON_A_FULL_DAY = 1


def validate_dates(state: dict) -> list[str]:
    """Dates parse, run the right way round, and match the stated duration."""
    errors = []
    start_raw, end_raw = state.get("start_date"), state.get("end_date")

    try:
        start = date.fromisoformat(start_raw) if start_raw else None
    except (TypeError, ValueError):
        return [f"Start date {start_raw!r} is not a valid date."]
    try:
        end = date.fromisoformat(end_raw) if end_raw else None
    except (TypeError, ValueError):
        return [f"End date {end_raw!r} is not a valid date."]

    if start is None or end is None:
        return ["The trip needs both a start and an end date."]

    if end < start:
        errors.append(f"End date ({end}) is before the start date ({start}).")
        return errors

    duration = state.get("duration_days")
    if duration and (end - start).days != duration - 1:
        errors.append(
            f"Dates {start} to {end} cover {(end - start).days + 1} days, "
            f"but the trip is {duration} days."
        )

    return errors


def validate_transport(state: dict) -> list[str]:
    """There is a way to get there, and it is one the traveller asked for."""
    transport = state.get("selected_transport") or {}
    if not transport:
        return [f"No transport was found from {state.get('origin')} to {state.get('destination')}."]

    errors = []
    if not transport.get("price_per_person") and not transport.get("total_price"):
        errors.append("The selected transport has no price.")

    limit = (state.get("preferences") or {}).get("max_journey_hours")
    duration = transport.get("duration_hours")
    if limit and duration and duration > limit:
        # Not an error on its own: the agent may have had no faster option. It is
        # only wrong if something within the limit existed and was passed over.
        alternatives = (state.get("transport_options") or {}).get("alternatives", [])
        if any(option.get("duration_hours", 0) <= limit for option in alternatives):
            errors.append(
                f"The chosen journey takes {duration}h, over the traveller's {limit}h "
                "limit, and a faster option was available."
            )

    return errors


def validate_hotel(state: dict) -> list[str]:
    """There is somewhere to stay, booked for the right number of nights."""
    hotel = state.get("selected_hotel") or {}
    if not hotel:
        return [f"No accommodation was found in {state.get('destination')}."]

    errors = []
    expected_nights = max((state.get("duration_days") or 1) - 1, 0)
    nights = hotel.get("nights")
    if nights is not None and nights != expected_nights:
        errors.append(
            f"The hotel is booked for {nights} nights but the trip needs {expected_nights}."
        )

    if not hotel.get("total_price") and not hotel.get("price_per_night"):
        errors.append("The selected hotel has no price.")

    return errors


def validate_itinerary(state: dict) -> list[str]:
    """One day per trip day, in order, on the right dates."""
    days = (state.get("itinerary") or {}).get("days", [])
    duration = state.get("duration_days") or 0

    if not days:
        return ["The plan has no itinerary."]

    errors = []
    if len(days) != duration:
        errors.append(f"Itinerary has {len(days)} days, expected {duration}.")

    if [day.get("day") for day in days] != list(range(1, len(days) + 1)):
        errors.append("Itinerary days are not numbered consecutively from 1.")

    start_raw = state.get("start_date")
    if start_raw:
        try:
            start = date.fromisoformat(start_raw)
        except ValueError:
            return errors
        for index, day in enumerate(days):
            expected = (start + timedelta(days=index)).isoformat()
            if day.get("date") != expected:
                errors.append(
                    f"Day {day.get('day')} is dated {day.get('date')}, expected {expected}."
                )

    return errors


def validate_daily_schedule(state: dict) -> list[str]:
    """Each day is actually plannable: it has content and no negative costs."""
    days = (state.get("itinerary") or {}).get("days", [])
    if not days:
        return []

    errors = []
    for day in days:
        label = f"Day {day.get('day')}"

        for slot in ("morning", "afternoon", "evening"):
            if not day.get(slot):
                errors.append(f"{label} has nothing planned for the {slot}.")

        for field in ("activity_cost", "local_transport_cost"):
            if (day.get(field) or 0) < 0:
                errors.append(f"{label} has a negative {field.replace('_', ' ')}.")

    # Travel days aside, an itinerary of entirely empty days is not a plan.
    middle = days[1:-1] if len(days) > 2 else []
    if middle and all(
        len(day.get("activities", [])) < MIN_ACTIVITIES_ON_A_FULL_DAY for day in middle
    ):
        errors.append("No activities were scheduled on any of the full days of the trip.")

    return errors


def validate_weather(state: dict) -> list[str]:
    """Outdoor plans on a wet day, but only when a drier day could take them.

    Same principle as the journey-time check: rain is not a defect in the plan,
    it is a defect only if we could have scheduled around it and did not.
    """
    days = (state.get("itinerary") or {}).get("days", [])
    forecast = {entry["date"]: entry for entry in state.get("weather", [])}
    outdoor = {place["name"] for place in state.get("places", []) if place.get("outdoor")}
    if not days or not forecast:
        return []

    # Mirrors what the re-planner can actually do: move an activity onto a dry
    # day that has room, or trade it for an indoor activity already sitting on
    # one. If neither is possible the rain is not a fixable defect.
    def is_fixable(day: dict) -> bool:
        if not forecast.get(day["date"], {}).get("good_for_outdoor"):
            return False
        has_room = len(day.get("activities", [])) < day_capacity(day["day"], len(days))
        can_trade = any(name not in outdoor for name in day.get("activities", []))
        return has_room or can_trade

    if not any(is_fixable(day) for day in days):
        return []

    errors = []
    for day in days:
        weather = forecast.get(day["date"])
        if not weather or weather.get("good_for_outdoor"):
            continue
        outdoors = [name for name in day.get("activities", []) if name in outdoor]
        if outdoors:
            errors.append(
                f"Day {day['day']} has outdoor plans ({', '.join(outdoors)}) in "
                f"{weather['condition']} ({weather['rain_probability']}% rain), and a "
                "drier day has room."
            )
    return errors


def validate_budget(state: dict, total_cost: float) -> list[str]:
    """The plan fits the money. Skipped when the traveller gave no budget."""
    budget = state.get("budget")
    if budget is None:
        return []
    if total_cost > budget:
        return [f"Budget exceeded by Rs {total_cost - budget:,.0f}."]
    return []


def validate_plan(state: dict, total_cost: float) -> list[str]:
    """Every check, in the order a person would read them."""
    return [
        *validate_dates(state),
        *validate_transport(state),
        *validate_hotel(state),
        *validate_itinerary(state),
        *validate_daily_schedule(state),
        *validate_weather(state),
        *validate_budget(state, total_cost),
    ]
