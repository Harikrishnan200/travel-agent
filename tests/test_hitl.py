"""Human in the loop: the interrupt, and the three ways out of it."""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.feedback import interpret_with_rules
from app.graph.graph import build_graph
from app.graph.state import new_state
from app.main import app

DEMO = "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 50000, I like food"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def graph():
    return build_graph(MemorySaver())


def plan(client, message=DEMO):
    return client.post("/api/v1/trips", json={"message": message}).json()


# --- the interrupt itself ---------------------------------------------------


def test_the_workflow_pauses_at_human_review(graph):
    config = {"configurable": {"thread_id": "pause"}}
    graph.invoke(new_state("pause", DEMO), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_review",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_the_pause_offers_the_plan_and_the_three_options(graph):
    config = {"configurable": {"thread_id": "offer"}}
    graph.invoke(new_state("offer", DEMO), config=config)

    pending = next(pending for task in graph.get_state(config).tasks for pending in task.interrupts)
    assert pending.value["options"] == ["approve", "modify", "reject"]
    assert "TRAVEL PLAN" in pending.value["summary"]


def test_approval_is_asked_once_not_per_tool_call(graph):
    """A whole trip is planned before anyone is interrupted."""
    config = {"configurable": {"thread_id": "once"}}
    state = graph.invoke(new_state("once", DEMO), config=config)
    assert state["selected_transport"]
    assert state["selected_hotel"]
    assert state["itinerary"]["days"]


# --- resuming ---------------------------------------------------------------


def test_approving_finalises_the_plan(graph):
    config = {"configurable": {"thread_id": "approve"}}
    graph.invoke(new_state("approve", DEMO), config=config)

    final = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert final["status"] == "approved"
    assert "TRAVEL PLAN" in final["final_response"]


def test_rejecting_ends_the_trip(graph):
    config = {"configurable": {"thread_id": "reject"}}
    graph.invoke(new_state("reject", DEMO), config=config)

    final = graph.invoke(Command(resume={"action": "reject"}), config=config)
    assert final["status"] == "rejected"
    assert "rejected" in final["final_response"]


def test_modifying_comes_back_for_review_rather_than_finishing(graph):
    config = {"configurable": {"thread_id": "modify"}}
    graph.invoke(new_state("modify", DEMO), config=config)

    graph.invoke(Command(resume={"action": "modify", "feedback": "cheaper hotel"}), config=config)
    snapshot = graph.get_state(config)
    assert snapshot.next == ("human_review",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_feedback_is_cleared_so_the_loop_does_not_repeat(graph):
    """Leaving human_feedback set would send every later review straight to re-planning."""
    config = {"configurable": {"thread_id": "cleared"}}
    graph.invoke(new_state("cleared", DEMO), config=config)
    graph.invoke(Command(resume={"action": "modify", "feedback": "cheaper hotel"}), config=config)

    assert graph.get_state(config).values["human_feedback"] == ""

    final = graph.invoke(Command(resume={"action": "approve"}), config=config)
    assert final["status"] == "approved"


# --- the feedback actually changes the plan ---------------------------------


def test_asking_for_a_cheaper_hotel_produces_a_cheaper_hotel(client):
    created = plan(client)
    before = created["plan"]["hotel"]["total_price"]

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "Reduce hotel cost"},
    ).json()

    assert after["plan"]["hotel"]["total_price"] < before
    assert after["total_cost"] < created["total_cost"]


def test_asking_for_a_new_interest_adds_those_places(client):
    created = plan(client)  # asked for food only
    trip_id = created["trip_id"]

    after = client.post(
        f"/api/v1/trips/{trip_id}/modify",
        json={"feedback": "add more historical places"},
    ).json()

    scheduled = [name for day in after["plan"]["itinerary"]["days"] for name in day["activities"]]
    assert any(
        name in scheduled
        for name in ("India Gate", "Red Fort", "Qutub Minar", "Jama Masjid", "Delhi Old Fort")
    )


def test_raising_the_budget_is_honoured(client):
    created = plan(client, "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 30000")
    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "increase the budget to 90000"},
    ).json()
    assert after["plan"]["budget"] == 90000


# --- the endpoints ----------------------------------------------------------


def test_a_new_plan_reports_that_it_is_waiting(client):
    created = plan(client)
    assert created["status"] == "awaiting_approval"
    assert created["awaiting_decision"]["options"] == ["approve", "modify", "reject"]


def test_an_approved_trip_is_no_longer_waiting(client):
    trip_id = plan(client)["trip_id"]
    approved = client.post(f"/api/v1/trips/{trip_id}/approve", json={"approved": True}).json()
    assert approved["status"] == "approved"
    assert approved["awaiting_decision"] is None


def test_approving_false_rejects(client):
    trip_id = plan(client)["trip_id"]
    response = client.post(f"/api/v1/trips/{trip_id}/approve", json={"approved": False}).json()
    assert response["status"] == "rejected"


def test_deciding_twice_is_a_conflict_not_a_crash(client):
    trip_id = plan(client)["trip_id"]
    client.post(f"/api/v1/trips/{trip_id}/approve", json={"approved": True})

    second = client.post(f"/api/v1/trips/{trip_id}/approve", json={"approved": True})
    assert second.status_code == 409
    assert "not waiting for a decision" in second.json()["detail"]


def test_deciding_on_an_unknown_trip_is_a_404(client):
    assert client.post("/api/v1/trips/nope/approve", json={"approved": True}).status_code == 404
    assert client.post("/api/v1/trips/nope/modify", json={"feedback": "x"}).status_code == 404


def test_empty_feedback_is_rejected(client):
    trip_id = plan(client)["trip_id"]
    assert client.post(f"/api/v1/trips/{trip_id}/modify", json={"feedback": ""}).status_code == 422


def test_a_message_is_treated_as_a_modification(client):
    trip_id = plan(client)["trip_id"]
    response = client.post(
        f"/api/v1/trips/{trip_id}/message", json={"message": "add more historical places"}
    ).json()
    assert response["status"] == "awaiting_approval"
    assert response["awaiting_decision"] is not None


# --- reading the feedback ---------------------------------------------------


def test_the_demo_feedback_is_read_as_two_separate_requests():
    intent = interpret_with_rules("Reduce hotel cost and add more historical places")
    assert intent.cheaper_hotel is True
    assert intent.add_interests == ["historical"]


def test_a_compliment_asks_for_nothing():
    intent = interpret_with_rules("This looks great, thanks")
    assert intent == interpret_with_rules("")


def test_cheaper_transport_is_distinguished_from_a_cheaper_hotel():
    intent = interpret_with_rules("make the flights cheaper")
    assert intent.cheaper_transport is True
    assert intent.cheaper_hotel is False


# --- a plan we could not rescue ---------------------------------------------


def test_an_impossible_plan_still_lets_the_traveller_answer(client):
    """Asking "would you like to raise the budget?" and then refusing the answer
    would be a dead end. It offers modify and reject instead."""
    created = plan(client, "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 5000")

    assert created["status"] == "needs_user_decision"
    assert created["awaiting_decision"]["options"] == ["modify", "reject"]
    assert created["awaiting_decision"]["problems"]


def test_an_impossible_plan_cannot_be_approved(client):
    """The output guard would reject it anyway; better to say so up front."""
    created = plan(client, "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 5000")

    response = client.post(f"/api/v1/trips/{created['trip_id']}/approve", json={"approved": True})
    assert response.status_code == 409
    assert "cannot be approved" in response.json()["detail"]


def test_raising_the_budget_rescues_an_impossible_plan(client):
    created = plan(client, "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 5000")
    trip_id = created["trip_id"]

    rescued = client.post(
        f"/api/v1/trips/{trip_id}/modify", json={"feedback": "increase the budget to 90000"}
    ).json()
    assert rescued["status"] == "awaiting_approval"
    assert rescued["total_cost"] <= rescued["plan"]["budget"]

    approved = client.post(f"/api/v1/trips/{trip_id}/approve", json={"approved": True}).json()
    assert approved["status"] == "approved"


def test_an_impossible_plan_can_still_be_rejected(client):
    created = plan(client, "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 5000")
    response = client.post(
        f"/api/v1/trips/{created['trip_id']}/approve", json={"approved": False}
    ).json()
    assert response["status"] == "rejected"
