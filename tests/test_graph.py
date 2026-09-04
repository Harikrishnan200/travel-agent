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


def test_human_review_routes_on_feedback_then_approval():
    assert route_after_human_review({"human_feedback": "cheaper hotel"}) == "modify"
    assert route_after_human_review({"human_approved": True}) == "approve"
    assert route_after_human_review({}) == "wait"


# --- re-planning and exhaustion -------------------------------------------


def test_tight_budget_triggers_replan_to_a_cheaper_hotel(graph):
    out = run(graph, trip_id="tight", budget=40000.0)
    assert out["replan_attempts"] >= 1
    assert out["selected_hotel"]["price_per_night"] == 1800  # downgraded


def test_impossible_budget_ends_in_needs_user_decision(graph):
    out = run(graph, trip_id="impossible", budget=1000.0)
    assert out["status"] == "needs_user_decision"
    assert out["replan_attempts"] == 3
    assert "budget" in out["final_response"].lower()


def test_replan_does_not_upgrade_the_hotel():
    state = {
        "replan_attempts": 0,
        "selected_hotel": {"name": "Budget Inn", "price_per_night": 1800},
        "hotel_options": [{"name": "Luxury", "price_per_night": 9000}],
    }
    assert "selected_hotel" not in nodes.replan(state)


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
