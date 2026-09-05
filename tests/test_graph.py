"""Workflow tests: state shape, cost arithmetic and the three routing decisions."""

import pytest

from app.graph import nodes
from app.graph.graph import (
    build_graph,
    route_after_human_review,
    route_after_plan_validation,
    route_after_request_validation,
)
from app.graph.state import STATUSES, new_state


@pytest.fixture
def graph():
    return build_graph()


def run(graph, trip_id="t1", **overrides):
    state = new_state(trip_id, "Plan a 5 day trip from Kochi to Delhi for 2 people")
    state.update(overrides)
    return graph.invoke(state, config={"configurable": {"thread_id": trip_id}})


# --- end to end ------------------------------------------------------------


def test_happy_path_reaches_human_review(graph):
    out = run(graph)
    assert out["status"] == "awaiting_approval"
    assert out["validation_errors"] == []
    assert len(out["itinerary"]["days"]) == out["duration_days"]


def test_status_is_always_a_known_status(graph):
    assert run(graph)["status"] in STATUSES


def test_itinerary_dates_are_contiguous_from_start_date(graph):
    days = run(graph)["itinerary"]["days"]
    assert [d["day"] for d in days] == list(range(1, len(days) + 1))
    assert len({d["date"] for d in days}) == len(days)


# --- cost arithmetic (Python owns this, never the LLM) ---------------------


def test_total_cost_is_the_sum_of_the_breakdown(graph):
    out = run(graph)
    assert out["total_cost"] == pytest.approx(sum(out["cost_breakdown"].values()))


def test_transport_cost_is_per_person_return():
    state = {
        "travelers": 2,
        "duration_days": 3,
        "selected_transport": {"price_per_person": 5000},
        "selected_hotel": {"price_per_night": 1000},
        "itinerary": {"days": [{"activity_cost": 100, "local_transport_cost": 50}] * 3},
    }
    costs = nodes.compute_costs(state)
    assert costs["cost_breakdown"]["transport"] == 20000  # 5000 x 2 people x 2 ways
    assert costs["cost_breakdown"]["hotel"] == 2000  # 2 nights for a 3 day trip
    assert costs["total_cost"] == 20000 + 2000 + 300 + 150


def test_provider_totals_take_precedence_over_per_unit_prices():
    """When a provider gives a total for the party, we trust it over our own maths."""
    state = {
        "travelers": 2,
        "duration_days": 3,
        "selected_transport": {"price_per_person": 5000, "total_price": 9000},
        "selected_hotel": {"price_per_night": 1000, "total_price": 4400},
        "itinerary": {"days": []},
    }
    costs = nodes.compute_costs(state)
    assert costs["cost_breakdown"]["transport"] == 18000  # 9000 each way
    assert costs["cost_breakdown"]["hotel"] == 4400


def test_every_day_of_the_itinerary_gets_a_plan(graph):
    for day in run(graph)["itinerary"]["days"]:
        assert day["morning"] and day["afternoon"] and day["evening"]
        assert day["local_transport_cost"] > 0


# --- routing ---------------------------------------------------------------


def test_invalid_request_stops_before_research():
    assert route_after_request_validation({"status": "invalid_request"}) == "invalid"
    assert route_after_request_validation({"status": "planning"}) == "valid"


def test_validation_failure_routes_to_replan():
    assert route_after_plan_validation({"validation_errors": ["over budget"]}) == "replan"


def test_validation_success_routes_to_human_review():
    assert route_after_plan_validation({"validation_errors": []}) == "valid"


def test_replanning_stops_after_max_attempts():
    state = {"validation_errors": ["over budget"], "replan_attempts": 3}
    assert route_after_plan_validation(state) == "exhausted"


def test_human_review_routes_on_the_travellers_decision():
    assert route_after_human_review({"human_feedback": "cheaper hotel"}) == "modify"
    assert route_after_human_review({"human_approved": True}) == "approve"
    assert route_after_human_review({"status": "rejected"}) == "reject"
    assert route_after_human_review({}) == "reject"


# --- re-planning and exhaustion -------------------------------------------


def test_a_tighter_budget_produces_a_cheaper_plan(graph):
    """The manager allocates the hotel budget up front, so most trips never need a re-plan."""
    roomy = run(graph, trip_id="roomy", budget=50000.0)
    tight = run(graph, trip_id="tight", budget=35000.0)

    assert tight["selected_hotel"]["total_price"] < roomy["selected_hotel"]["total_price"]
    assert tight["total_cost"] < roomy["total_cost"]
    assert tight["status"] == "awaiting_approval"


def test_impossible_budget_ends_in_needs_user_decision(graph):
    """Return airfare alone exceeds this budget, so no re-plan can rescue it."""
    out = run(graph, trip_id="impossible", budget=1000.0)
    assert out["status"] == "needs_user_decision"
    assert out["replan_attempts"] == 3
    assert "budget" in out["final_response"].lower()


def test_a_plan_that_fits_is_delivered_within_budget(graph):
    """Re-planning may still run for the weather, but the money must come out right."""
    out = run(graph, trip_id="fits", budget=50000.0)
    assert out["total_cost"] <= out["budget"]
    assert out["status"] == "awaiting_approval"
    assert out["validation_errors"] == []


def test_replan_does_not_upgrade_the_hotel():
    state = {
        "replan_attempts": 0,
        "validation_errors": ["Budget exceeded by Rs 1,000."],
        "selected_hotel": {"name": "Budget Inn", "total_price": 1800},
        "hotel_options": [{"name": "Luxury", "total_price": 9000}],
    }
    assert "selected_hotel" not in nodes.replan(state)


def test_replan_delegates_to_the_lever_module():
    """The node is a one-liner; the levers are tested in test_replan.py."""
    state = {
        "replan_attempts": 0,
        "validation_errors": ["Budget exceeded by Rs 1,000."],
        "selected_hotel": {"name": "Expensive", "total_price": 9000},
        "hotel_options": [
            {"name": "Mid", "total_price": 5000, "rating": 4.0},
            {"name": "Cheap", "total_price": 2000, "rating": 3.2},
        ],
    }
    updates = nodes.replan(state)
    assert updates["selected_hotel"]["name"] == "Mid"
    assert updates["replan_attempts"] == 1


# --- output guard ----------------------------------------------------------


def test_output_guard_rejects_a_tampered_total():
    state = {
        "travelers": 1,
        "duration_days": 2,
        "selected_transport": {"price_per_person": 1000},
        "selected_hotel": {"price_per_night": 500},
        "itinerary": {"days": [{"activity_cost": 0, "local_transport_cost": 0}] * 2},
        "total_cost": 999999.0,
    }
    assert nodes.output_guard(state)["status"] == "failed"
