"""Small rules shared across the codebase, kept here to avoid circular imports.

app/graph/nodes.py imports app/agents/travel_manager.py, so a rule both of them
need cannot live in either without one importing the other. This module has no
dependencies of its own, so anything goes here instead.
"""

# A full day: morning, afternoon, evening.
ACTIVITIES_PER_DAY = 3

# Arrival and departure days are partly spent travelling.
ACTIVITIES_ON_A_TRAVEL_DAY = 1


def day_capacity(day_number: int, total_days: int) -> int:
    """How many activities day `day_number` (1-based) of a `total_days` trip can hold."""
    if total_days > 1 and day_number in (1, total_days):
        return ACTIVITIES_ON_A_TRAVEL_DAY
    return ACTIVITIES_PER_DAY


def is_travel_day(day_number: int, total_days: int) -> bool:
    """Arrival and departure days, which are allowed to hold nothing at all."""
    return total_days > 1 and day_number in (1, total_days)


def round_trip_transport_cost(transport: dict, travelers: int) -> float:
    """The fare for everyone, there and back.

    A provider's own total_price is trusted over price_per_person * travelers
    when it is present, because a real fare is not always a clean multiple of
    the per-person price (group rates, rounding). Used by compute_costs (the
    validator, the re-planner and the output guard all call that) and by the
    Travel Manager's hotel-budget allocation - previously two separate
    formulas that only agreed because every provider happened to set
    total_price to exactly price_per_person * travelers. Nothing enforced
    that, so it was a bug waiting for a provider that did not.
    """
    one_way = transport.get("total_price") or transport.get("price_per_person", 0) * travelers
    return float(one_way) * 2
