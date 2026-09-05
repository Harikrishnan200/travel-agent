"""Re-planning: the four levers, and the order they are tried in."""

from app.graph.nodes import compute_costs
from app.validation import validators
from app.validation.replan import replan

BUDGET_ERROR = ["Budget exceeded by Rs 7,000."]

HOTELS = [
    {"name": "Mid", "total_price": 6000, "rating": 4.0},
    {"name": "Cheap", "total_price": 2000, "rating": 3.2},
]

ALTERNATIVES = [
    {"id": "F2", "mode": "flight", "price_per_person": 4000, "duration_hours": 3},
    {"id": "T1", "mode": "train", "price_per_person": 1200, "duration_hours": 34},
]


def state(**overrides):
    base = {
        "travelers": 2,
        "duration_days": 3,
        "budget": 20000.0,
        "preferences": {},
        "validation_errors": BUDGET_ERROR,
        "replan_attempts": 0,
        "selected_hotel": {"name": "Expensive", "total_price": 9000, "rating": 4.6},
        "hotel_options": [dict(hotel) for hotel in HOTELS],
        "selected_transport": {"id": "F1", "mode": "flight", "price_per_person": 6000},
        "transport_options": {"alternatives": [dict(o) for o in ALTERNATIVES]},
        "places": [],
        "weather": [],
        "itinerary": {"days": []},
    }
    return {**base, **overrides}


def run(**overrides):
    return replan(state(**overrides), compute_costs)


# --- the order of the levers ------------------------------------------------


def test_the_hotel_is_the_first_thing_downgraded():
    """The cheapest change to the traveller's experience comes first."""
    updates = run()
    assert updates["selected_hotel"]["name"] == "Mid"  # best rated of the cheaper options
    assert "selected_transport" not in updates


def test_transport_is_only_touched_once_the_hotel_cannot_help():
    updates = run(hotel_options=[])
    assert updates["selected_transport"]["id"] == "T1"
    assert "selected_hotel" not in updates


def test_each_attempt_counts():
    assert run(replan_attempts=2)["replan_attempts"] == 3


def test_nothing_is_changed_when_the_plan_is_valid():
    updates = run(validation_errors=[])
    assert set(updates) == {"replan_attempts"}


# --- lever 1: the hotel -----------------------------------------------------


def test_a_used_hotel_is_removed_from_the_pool():
    """So the next attempt tries something different instead of looping."""
    updates = run()
    assert [hotel["name"] for hotel in updates["hotel_options"]] == ["Cheap"]


def test_the_hotel_is_never_upgraded():
    updates = run(
        selected_hotel={"name": "Cheap", "total_price": 2000},
        hotel_options=[{"name": "Luxury", "total_price": 90000, "rating": 5.0}],
    )
    assert "selected_hotel" not in updates


# --- lever 2: transport -----------------------------------------------------


def test_a_cheaper_option_within_the_time_limit_is_preferred():
    """The traveller's stated limit survives a budget squeeze if anything fits it."""
    updates = run(hotel_options=[], preferences={"max_journey_hours": 12})
    assert updates["selected_transport"]["id"] == "F2"
    assert "over the" not in updates["selected_transport"]["reason"]


def test_breaking_the_time_limit_is_allowed_but_stated_plainly():
    cheap_but_slow = [{"id": "T1", "mode": "train", "price_per_person": 1200, "duration_hours": 34}]
    updates = run(
        hotel_options=[],
        preferences={"max_journey_hours": 12},
        transport_options={"alternatives": cheap_but_slow},
    )
    assert updates["selected_transport"]["id"] == "T1"
    assert "over the 12h you asked for" in updates["selected_transport"]["reason"]


def test_transport_is_not_swapped_for_something_dearer():
    updates = run(
        hotel_options=[],
        selected_transport={"id": "T1", "mode": "train", "price_per_person": 500},
    )
    assert "selected_transport" not in updates


# --- lever 3: activities ----------------------------------------------------

DAYS = [
    {"day": 1, "activities": ["Free Park"], "activity_cost": 0, "notes": ""},
    {"day": 2, "activities": ["Red Fort", "Museum"], "activity_cost": 1600, "notes": ""},
    {"day": 3, "activities": ["Market"], "activity_cost": 0, "notes": ""},
]
PLACES = [
    {"name": "Red Fort", "entry_fee": 600, "outdoor": True},
    {"name": "Museum", "entry_fee": 200, "outdoor": False},
    {"name": "Free Park", "entry_fee": 0, "outdoor": True},
    {"name": "Market", "entry_fee": 0, "outdoor": False},
    {"name": "Riverside Walk", "entry_fee": 0, "outdoor": True},
]


def test_the_priciest_activity_is_swapped_for_a_free_one():
    updates = run(
        hotel_options=[],
        transport_options={"alternatives": []},
        itinerary={"days": [dict(day) for day in DAYS]},
        places=PLACES,
    )
    day_two = updates["itinerary"]["days"][1]
    assert "Red Fort" not in day_two["activities"]
    assert "Riverside Walk" in day_two["activities"]
    assert day_two["activity_cost"] == 1600 - 600 * 2  # fee times travellers


def test_with_no_free_alternative_the_activity_is_dropped():
    updates = run(
        hotel_options=[],
        transport_options={"alternatives": []},
        itinerary={"days": [dict(day) for day in DAYS]},
        places=PLACES[:2],
    )
    day_two = updates["itinerary"]["days"][1]
    assert day_two["activities"] == ["Museum"]
    assert "Dropped Red Fort" in day_two["notes"]


def test_a_plan_with_nothing_left_to_cut_changes_nothing():
    free_only = [{"day": 1, "activities": ["Market"], "activity_cost": 0, "notes": ""}]
    updates = run(
        hotel_options=[],
        transport_options={"alternatives": []},
        itinerary={"days": free_only},
        places=PLACES,
    )
    assert set(updates) == {"replan_attempts"}


# --- lever 4: the weather ---------------------------------------------------

WEATHER_DAYS = [
    {"day": 1, "date": "2026-09-18", "activities": ["Museum"], "notes": ""},
    {"day": 2, "date": "2026-09-19", "activities": ["Red Fort"], "notes": ""},
    {"day": 3, "date": "2026-09-20", "activities": ["Market"], "notes": ""},
]
FORECAST = [
    {"date": "2026-09-18", "rain_probability": 90, "good_for_outdoor": False, "condition": "rain"},
    {"date": "2026-09-19", "rain_probability": 95, "good_for_outdoor": False, "condition": "rain"},
    {"date": "2026-09-20", "rain_probability": 10, "good_for_outdoor": True, "condition": "clear"},
]
RAIN_ERROR = ["Day 2 has outdoor plans (Red Fort) in rain (95% rain), and a drier day has room."]


def weather_state(**overrides):
    return {
        "validation_errors": RAIN_ERROR,
        "itinerary": {"days": [dict(day) for day in WEATHER_DAYS]},
        "weather": FORECAST,
        "places": PLACES,
        "duration_days": 3,
        **overrides,
    }


def test_an_outdoor_activity_moves_to_the_dry_day():
    updates = replan(weather_state(), compute_costs)
    days = {day["day"]: day for day in updates["itinerary"]["days"]}
    assert "Red Fort" in days[3]["activities"]
    assert "Red Fort" not in days[2]["activities"]


def test_the_dry_day_gives_back_an_indoor_activity():
    """A move without a trade would leave the wet day empty."""
    updates = replan(weather_state(), compute_costs)
    days = {day["day"]: day for day in updates["itinerary"]["days"]}
    assert "Market" in days[2]["activities"]
    assert days[2]["activities"]  # never emptied


def test_the_swap_is_explained_in_the_day_notes():
    updates = replan(weather_state(), compute_costs)
    day_two = updates["itinerary"]["days"][1]
    assert "Moved Red Fort to day 3" in day_two["notes"]


def test_the_daily_slots_follow_the_new_activities():
    updates = replan(weather_state(), compute_costs)
    days = {day["day"]: day for day in updates["itinerary"]["days"]}
    assert days[3]["morning"] == "Red Fort"
    assert days[2]["morning"] == "Market"


def test_a_departure_day_may_be_emptied_to_save_an_outdoor_plan():
    """Travel days are allowed to hold nothing, so moving their last activity is fine."""
    days = [
        {"day": 1, "date": "2026-09-18", "activities": ["Museum"], "notes": ""},
        {"day": 2, "date": "2026-09-19", "activities": ["Market"], "notes": ""},
        {"day": 3, "date": "2026-09-20", "activities": ["Red Fort"], "notes": ""},
    ]
    forecast = [
        {"date": "2026-09-18", "rain_probability": 90, "good_for_outdoor": False, "condition": "r"},
        {"date": "2026-09-19", "rain_probability": 10, "good_for_outdoor": True, "condition": "s"},
        {"date": "2026-09-20", "rain_probability": 90, "good_for_outdoor": False, "condition": "r"},
    ]
    updates = replan(weather_state(itinerary={"days": days}, weather=forecast), compute_costs)
    moved = {day["day"]: day["activities"] for day in updates["itinerary"]["days"]}
    assert "Red Fort" in moved[2]


def test_a_full_day_is_never_emptied():
    """Unlike a travel day, a middle day must keep something to do."""
    days = [
        {"day": 1, "date": "2026-09-18", "activities": ["Museum"], "notes": ""},
        {"day": 2, "date": "2026-09-19", "activities": ["Red Fort"], "notes": ""},
        {"day": 3, "date": "2026-09-20", "activities": ["Free Park"], "notes": ""},
        {"day": 4, "date": "2026-09-21", "activities": ["Market"], "notes": ""},
    ]
    forecast = [
        {"date": "2026-09-18", "rain_probability": 90, "good_for_outdoor": False, "condition": "r"},
        {"date": "2026-09-19", "rain_probability": 90, "good_for_outdoor": False, "condition": "r"},
        {"date": "2026-09-20", "rain_probability": 10, "good_for_outdoor": True, "condition": "s"},
        {"date": "2026-09-21", "rain_probability": 90, "good_for_outdoor": False, "condition": "r"},
    ]
    updates = replan(
        weather_state(itinerary={"days": days}, weather=forecast, duration_days=4), compute_costs
    )
    if "itinerary" in updates:
        for day in updates["itinerary"]["days"]:
            if day["day"] == 2:  # a full day, so it must keep something
                assert day["activities"]


def test_nothing_moves_when_every_day_is_wet():
    all_wet = [{**entry, "good_for_outdoor": False} for entry in FORECAST]
    updates = replan(weather_state(weather=all_wet), compute_costs)
    assert "itinerary" not in updates


def test_a_travel_day_is_not_loaded_past_its_capacity():
    """Day 1 is dry but is the arrival day, so it holds one activity only."""
    forecast = [
        {"date": "2026-09-18", "rain_probability": 5, "good_for_outdoor": True, "condition": "sun"},
        {"date": "2026-09-19", "rain_probability": 95, "good_for_outdoor": False, "condition": "r"},
        {"date": "2026-09-20", "rain_probability": 95, "good_for_outdoor": False, "condition": "r"},
    ]
    days = [
        {"day": 1, "date": "2026-09-18", "activities": ["Free Park"], "notes": ""},
        {"day": 2, "date": "2026-09-19", "activities": ["Red Fort", "Museum"], "notes": ""},
        {"day": 3, "date": "2026-09-20", "activities": ["Market"], "notes": ""},
    ]
    updates = replan(weather_state(weather=forecast, itinerary={"days": days}), compute_costs)
    if "itinerary" in updates:
        day_one = updates["itinerary"]["days"][0]
        assert len(day_one["activities"]) <= 1


# --- the validator that triggers the weather lever --------------------------


def test_rain_is_only_an_error_when_the_re_planner_could_fix_it():
    """The validator flags exactly what the re-planner can act on, and nothing more."""
    plan = {
        "itinerary": {"days": [dict(day) for day in WEATHER_DAYS]},
        "weather": FORECAST,
        "places": PLACES,
        "duration_days": 3,
    }
    # Day 3 is dry and holds an indoor activity, so a trade is available.
    assert validators.validate_weather(plan)

    # Now the dry day is a full travel day holding only outdoor plans: there is
    # no room to move into and nothing indoors to trade back.
    unfixable = [dict(day) for day in WEATHER_DAYS]
    unfixable[2]["activities"] = ["Free Park"]
    assert validators.validate_weather({**plan, "itinerary": {"days": unfixable}}) == []


def test_the_validator_and_the_re_planner_agree():
    """Anything flagged as fixable must actually get fixed, or the loop spins."""
    plan = {
        "itinerary": {"days": [dict(day) for day in WEATHER_DAYS]},
        "weather": FORECAST,
        "places": PLACES,
        "duration_days": 3,
        "validation_errors": RAIN_ERROR,
    }
    assert validators.validate_weather(plan)
    assert "itinerary" in replan(plan, compute_costs)
