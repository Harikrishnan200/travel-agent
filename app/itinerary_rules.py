"""How many activities a day can hold.

Shared by the Experience Agent (which fills days), the validator (which checks
them) and the re-planner (which moves activities between them). Keeping the rule
in one place stops the three from disagreeing about what a full day looks like.
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
