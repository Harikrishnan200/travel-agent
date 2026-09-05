"""Deterministic validation.

These are the rules that decide whether a plan is real. No LLM is involved, so
every case here is exact.
"""

import pytest

from app.validation import validators

BASE = {
    "origin": "Kochi",
    "destination": "Delhi",
    "start_date": "2026-09-18",
    "end_date": "2026-09-22",
    "duration_days": 5,
    "travelers": 2,
    "budget": 50000.0,
    "preferences": {},
    "selected_transport": {"price_per_person": 6000, "duration_hours": 3, "total_price": 12000},
    "selected_hotel": {"price_per_night": 3000, "nights": 4, "total_price": 12000},
    "itinerary": {
        "days": [
            {
                "day": index + 1,
                "date": f"2026-09-{18 + index}",
                "morning": "a",
                "afternoon": "b",
                "evening": "c",
                "activities": ["Red Fort"],
                "activity_cost": 200,
                "local_transport_cost": 500,
            }
            for index in range(5)
        ]
    },
}


def plan(**overrides):
    return {**BASE, **overrides}


# --- a sound plan passes ----------------------------------------------------


def test_a_sound_plan_has_no_errors():
    assert validators.validate_plan(plan(), total_cost=40000) == []


# --- dates ------------------------------------------------------------------


def test_an_end_date_before_the_start_is_caught():
    errors = validators.validate_dates(plan(start_date="2026-09-22", end_date="2026-09-18"))
    assert "before the start date" in errors[0]


def test_dates_that_do_not_match_the_duration_are_caught():
    errors = validators.validate_dates(plan(end_date="2026-09-25"))
    assert "but the trip is 5 days" in errors[0]


def test_an_unparseable_date_is_caught_not_crashed_on():
    assert "not a valid date" in validators.validate_dates(plan(start_date="18/09/2026"))[0]


def test_missing_dates_are_caught():
    assert validators.validate_dates(plan(start_date=None, end_date=None))


def test_a_single_day_trip_is_valid():
    assert (
        validators.validate_dates(
            plan(start_date="2026-09-18", end_date="2026-09-18", duration_days=1)
        )
        == []
    )


# --- transport --------------------------------------------------------------


def test_a_plan_with_no_transport_is_rejected():
    assert "No transport" in validators.validate_transport(plan(selected_transport={}))[0]


def test_transport_with_no_price_is_rejected():
    errors = validators.validate_transport(plan(selected_transport={"duration_hours": 3}))
    assert "no price" in errors[0]


def test_exceeding_the_journey_limit_is_only_an_error_if_something_faster_existed():
    """The agent cannot be blamed for a limit no option could meet."""
    slow = plan(
        preferences={"max_journey_hours": 12},
        selected_transport={"total_price": 1000, "duration_hours": 34},
        transport_options={"alternatives": [{"duration_hours": 40}]},
    )
    assert validators.validate_transport(slow) == []

    with_faster = {**slow, "transport_options": {"alternatives": [{"duration_hours": 3}]}}
    assert "faster option was available" in validators.validate_transport(with_faster)[0]


# --- hotel ------------------------------------------------------------------


def test_a_plan_with_no_hotel_is_rejected():
    assert "No accommodation" in validators.validate_hotel(plan(selected_hotel={}))[0]


def test_a_hotel_booked_for_the_wrong_number_of_nights_is_caught():
    errors = validators.validate_hotel(plan(selected_hotel={"total_price": 1, "nights": 2}))
    assert "2 nights but the trip needs 4" in errors[0]


# --- itinerary --------------------------------------------------------------


def test_a_missing_itinerary_is_caught():
    assert validators.validate_itinerary(plan(itinerary={})) == ["The plan has no itinerary."]


def test_the_wrong_number_of_days_is_caught():
    short = plan(itinerary={"days": BASE["itinerary"]["days"][:3]})
    assert "has 3 days, expected 5" in validators.validate_itinerary(short)[0]


def test_days_out_of_order_are_caught():
    shuffled = [dict(day) for day in BASE["itinerary"]["days"]]
    shuffled[0]["day"], shuffled[1]["day"] = 2, 1
    errors = validators.validate_itinerary(plan(itinerary={"days": shuffled}))
    assert any("consecutively" in error for error in errors)


def test_a_day_on_the_wrong_date_is_caught():
    wrong = [dict(day) for day in BASE["itinerary"]["days"]]
    wrong[2]["date"] = "2026-10-01"
    errors = validators.validate_itinerary(plan(itinerary={"days": wrong}))
    assert any("expected 2026-09-20" in error for error in errors)


# --- daily schedule ---------------------------------------------------------


def test_an_empty_slot_is_caught():
    days = [dict(day) for day in BASE["itinerary"]["days"]]
    days[1]["afternoon"] = ""
    errors = validators.validate_daily_schedule(plan(itinerary={"days": days}))
    assert "Day 2 has nothing planned for the afternoon." in errors


def test_a_negative_cost_is_caught():
    days = [dict(day) for day in BASE["itinerary"]["days"]]
    days[0]["activity_cost"] = -100
    errors = validators.validate_daily_schedule(plan(itinerary={"days": days}))
    assert any("negative activity cost" in error for error in errors)


def test_a_trip_with_no_activities_on_any_full_day_is_caught():
    days = [{**day, "activities": []} for day in BASE["itinerary"]["days"]]
    errors = validators.validate_daily_schedule(plan(itinerary={"days": days}))
    assert any("No activities were scheduled" in error for error in errors)


def test_empty_arrival_and_departure_days_are_acceptable():
    """Those days are spent travelling, so they are allowed to be quiet."""
    days = [dict(day) for day in BASE["itinerary"]["days"]]
    days[0]["activities"] = []
    days[-1]["activities"] = []
    assert validators.validate_daily_schedule(plan(itinerary={"days": days})) == []


# --- budget -----------------------------------------------------------------


def test_going_over_budget_is_reported_with_the_amount():
    assert validators.validate_budget(plan(), 57000) == ["Budget exceeded by Rs 7,000."]


def test_exactly_on_budget_is_allowed():
    assert validators.validate_budget(plan(), 50000) == []


def test_no_budget_means_no_budget_error():
    assert validators.validate_budget(plan(budget=None), 999999) == []


@pytest.mark.parametrize("total", [0, 1, 49999.99])
def test_anything_under_budget_passes(total):
    assert validators.validate_budget(plan(), total) == []
