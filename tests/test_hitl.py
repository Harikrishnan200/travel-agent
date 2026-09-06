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


def test_asking_for_more_of_an_existing_interest_changes_the_plan(client):
    """The brief's own demo feedback, and it used to do nothing.

    "Add more historical places" when historical is already an interest merged a
    list with itself, so the plan came back identical while claiming to have been
    modified.
    """
    historical = {
        "India Gate",
        "Red Fort",
        "Qutub Minar",
        "Humayun's Tomb",
        "Jama Masjid",
        "Delhi Old Fort",
        "Delhi Stepwell",
        "Heritage Walk in Delhi",
    }

    def count(plan):
        scheduled = [a for day in plan["itinerary"]["days"] for a in day["activities"]]
        return sum(1 for name in scheduled if name in historical)

    created = plan(
        client,
        "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 60000, "
        "I like historical places and food",
    )
    before = count(created["plan"])

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "add more historical places"},
    ).json()

    assert count(after["plan"]) > before


def test_emphasis_outranks_the_weather_preference(client):
    """The traveller asked; the weather is advice. The validator still flags it."""
    created = plan(
        client,
        "Plan a 5 day trip from Kochi to Delhi for 2 people under Rs 60000, I like food",
    )

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "add more historical places"},
    ).json()

    scheduled = [a for day in after["plan"]["itinerary"]["days"] for a in day["activities"]]
    assert any(
        name in {"India Gate", "Red Fort", "Qutub Minar", "Humayun's Tomb", "Jama Masjid"}
        for name in scheduled
    )


# --- changing the traveller count ------------------------------------------


def test_reducing_travelers_halves_per_head_costs(client):
    """The bug report: transport and hotel totals must actually track headcount.

    Before this fix, FeedbackIntent had no field for a traveller-count change
    at all, so "reduce the number of people to 2" silently did nothing - the
    plan stayed priced for the original headcount.
    """
    created = plan(
        client,
        "Plan a 5 day trip from Kochi to Bangalore for 4 people under Rs 20000, " "I like food",
    )
    before = created["plan"]

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "i reduced the number of peoples to 2"},
    ).json()
    updated = after["plan"]

    assert updated["travelers"] == 2
    assert updated["hotel"]["rooms_required"] < before["hotel"]["rooms_required"]
    assert updated["transport"]["total_price"] < before["transport"]["total_price"]
    assert after["total_cost"] < created["total_cost"]


def test_a_traveler_change_rescues_a_plan_that_did_not_fit(client):
    """Fewer people should be able to turn an impossible plan into a valid one."""
    created = plan(
        client,
        "Plan a 5 day trip from Kochi to Bangalore for 4 people under Rs 20000",
    )
    assert created["status"] == "needs_user_decision"

    rescued = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "actually it's only 2 people now"},
    ).json()

    assert rescued["status"] == "awaiting_approval"
    assert rescued["plan"]["travelers"] == 2


def test_a_money_lever_is_not_undone_by_a_full_re_research(client):
    """Asking for a cheaper hotel must not be silently reselected away.

    Guards the fix above: routing modify through the full research pipeline
    when travelers changes must not also blow away the direct "pick a cheaper
    hotel" lever on a request that only asked for that.
    """
    created = plan(client)
    before_price = created["plan"]["hotel"]["total_price"]

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "reduce the hotel cost"},
    ).json()

    assert after["plan"]["hotel"]["total_price"] < before_price
    assert after["plan"]["travelers"] == created["plan"]["travelers"]


def test_the_rule_based_reader_understands_a_traveler_change():
    from app.graph.feedback import interpret_with_rules

    intent = interpret_with_rules("i reduced the number of peoples to 2")
    assert intent.new_travelers == 2


def test_reduce_without_a_headcount_is_read_as_a_money_request_not_a_traveler_change():
    """ "Reduce the hotel cost" must not be misread as a people-count change."""
    from app.graph.feedback import interpret_with_rules

    intent = interpret_with_rules("reduce the hotel cost please")
    assert intent.new_travelers is None
    assert intent.cheaper_hotel is True


def test_a_stale_over_budget_message_does_not_survive_a_fix(client):
    """The exact reported bug: needs_user_decision leaves final_response set,
    and a later modify that fixes the budget skips that node entirely on its
    way back to human_review - so nothing else would ever clear it out.
    """
    impossible = plan(
        client, "Plan a 5 day trip from Kochi to Bangalore for 4 people under Rs 20000"
    )
    assert impossible["status"] == "needs_user_decision"
    assert "budget" in impossible["final_response"].lower()

    rescued = client.post(
        f"/api/v1/trips/{impossible['trip_id']}/modify",
        json={"feedback": "reduce the number of people to 2"},
    ).json()

    assert rescued["status"] == "awaiting_approval"
    assert rescued["validation_errors"] == []
    assert rescued["final_response"] is None
    assert rescued["awaiting_decision"]["message"] == ""
    assert rescued["awaiting_decision"]["options"] == ["approve", "modify", "reject"]


# --- bugs found by systematically hunting for the same "stale state" class --


def test_a_recovered_tool_failure_is_no_longer_reported(client):
    """A search that failed once and later succeeded must not go on looking
    broken for the rest of the trip - the exact bug reported: "train search
    unavailable" was still showing up after a later modify that used trains
    successfully.
    """
    from unittest.mock import patch

    from gateway import mcp_router

    working = mcp_router.call_tool

    def trains_down(tool, arguments):
        if tool == "search_trains":
            raise mcp_router.MCPUnavailableError("down")
        return working(tool, arguments)

    with patch.object(mcp_router, "call_tool", trains_down):
        created = plan(
            client,
            "Plan a 5 day trip from Kochi to Delhi for 4 people under Rs 60000",
        )
    assert "train search unavailable" in created["degraded"]

    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "reduce to 2 people"},
    ).json()
    assert "train search unavailable" not in after["degraded"]


def test_a_persistent_tool_failure_is_still_reported(client):
    """The other half of the fix: clearing a stale notice must not also
    swallow a real, ongoing problem."""
    from unittest.mock import patch

    from gateway import mcp_router

    working = mcp_router.call_tool

    def trains_down(tool, arguments):
        if tool == "search_trains":
            raise mcp_router.MCPUnavailableError("down")
        return working(tool, arguments)

    with patch.object(mcp_router, "call_tool", trains_down):
        created = plan(
            client,
            "Plan a 5 day trip from Kochi to Delhi for 4 people under Rs 60000",
        )
        assert "train search unavailable" in created["degraded"]

        after = client.post(
            f"/api/v1/trips/{created['trip_id']}/modify",
            json={"feedback": "reduce to 2 people"},
        ).json()
        assert "train search unavailable" in after["degraded"]


def test_a_same_day_trip_needs_no_hotel(client):
    """A day trip has nowhere to check in to. Used to always fail: the tool
    correctly refused a zero-night search, and that refusal was reported as
    "no accommodation was found" instead of "none was needed".
    """
    created = plan(client, "Plan a 1 day trip from Kochi to Delhi for 2 people under Rs 60000")

    assert created["status"] == "awaiting_approval"
    assert created["plan"]["hotel"] in ({}, None)
    assert created["plan"]["cost_breakdown"]["hotel"] == 0
    assert "hotel search unavailable" not in created["degraded"]


def test_a_traveler_change_beyond_the_limit_is_refused_with_a_clear_reason(client):
    """Before this fix, the state was corrupted to travelers=500 anyway, and
    the traveller saw a confusing cascade - "no transport found", "no
    accommodation found", "budget exceeded by Rs 900,000+" - instead of one
    clear reason nothing changed.
    """
    created = plan(client)
    after = client.post(
        f"/api/v1/trips/{created['trip_id']}/modify",
        json={"feedback": "we are now 500 people"},
    ).json()

    assert after["plan"]["travelers"] == created["plan"]["travelers"]
    assert any("must be between 1 and" in note for note in after["degraded"])
    assert after["status"] == "awaiting_approval"


def test_a_negative_budget_is_refused_rather_than_applied(client):
    """The guard in apply_feedback, tested directly against a crafted intent -
    natural language rarely spells out a negative number, but an LLM (or a
    parsing quirk) can still hand one back, and it must not be applied as if
    it were a real budget cut.
    """
    from unittest.mock import patch

    from app.graph import feedback as feedback_module
    from app.graph.feedback import FeedbackIntent
    from app.llm.client import LLMUnavailable

    created = plan(client)

    with patch.object(feedback_module, "interpret_with_llm", side_effect=LLMUnavailable("x")):
        with patch.object(
            feedback_module,
            "interpret_with_rules",
            return_value=FeedbackIntent(new_budget=-5000.0),
        ):
            after = client.post(
                f"/api/v1/trips/{created['trip_id']}/modify",
                json={"feedback": "irrelevant, the intent above is forced"},
            ).json()

    assert after["plan"]["budget"] == created["plan"]["budget"]
    assert any("positive amount" in note for note in after["degraded"])


def test_the_rule_based_budget_reader_does_not_understand_a_minus_sign():
    """Documents a real, minor gap found while testing the guard above: the
    regex drops the sign, so "-5000" reads as a plain 5000. Not worth fixing
    for how people actually phrase a budget - nobody sincerely asks for a
    negative one - but worth knowing next time this code is touched.
    """
    from app.graph.feedback import interpret_with_rules

    assert interpret_with_rules("reduce the budget to -5000").new_budget == 5000.0
