"""Reading requirements out of a traveller's message.

The rule-based reader is tested directly. The LLM path is tested through the
workflow node, where what matters is that a degraded extraction is recorded as
one rather than passing for a normal result.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.graph import extraction, nodes
from app.graph.extraction import ExtractedRequest, extract_with_rules, to_state
from app.llm.client import LLMResult, LLMUnavailable

DEMO = (
    "Plan a 5 day trip from Kochi to Delhi for 2 people. Budget Rs 50,000. "
    "I prefer trains if the journey is under 12 hours, otherwise use flights. "
    "I like historical places and food."
)


# --- the rule-based reader --------------------------------------------------


def test_the_demo_request_is_read_correctly():
    extracted = extract_with_rules(DEMO)
    assert extracted.origin == "Kochi"
    assert extracted.destination == "Delhi"
    assert extracted.duration_days == 5
    assert extracted.travelers == 2
    assert extracted.budget == 50000
    assert extracted.transport_mode == "train"
    assert extracted.max_journey_hours == 12
    assert extracted.interests == ["food", "historical"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("budget 50k", 50000),
        ("budget Rs 50,000", 50000),
        ("under 1.5 lakh", 150000),
        ("within ₹25000", 25000),
        ("a nice trip", None),
        # A time limit is not an amount of money.
        ("trip under 12 hours by train", None),
        ("prefer trains under 12 hours, budget 30000", 30000),
        # Nor is a trip length.
        ("under 5 days", None),
    ],
)
def test_budgets_are_read_in_the_forms_people_write_them(message, expected):
    assert extract_with_rules(message).budget == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [("for 2 people", 2), ("for 2, please", 2), ("for 5 days", None)],
)
def test_a_bare_count_is_travellers_but_a_duration_is_not(message, expected):
    assert extract_with_rules(message).travelers == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [("for 4 people", 4), ("couple trip", 2), ("solo trip", 1), ("a trip", None)],
)
def test_traveller_counts_are_read(message, expected):
    assert extract_with_rules(message).travelers == expected


def test_from_and_to_decide_the_direction():
    """Two cities named in the other order must not flip the trip."""
    assert extract_with_rules("I want to go from Delhi to Kochi").origin == "Delhi"
    assert extract_with_rules("Take me to Kochi, leaving from Delhi").destination == "Kochi"


def test_a_single_city_is_read_as_the_destination():
    extracted = extract_with_rules("3 day trip to Goa")
    assert extracted.destination == "Goa"
    assert extracted.origin is None


def test_a_non_travel_message_is_flagged():
    assert extract_with_rules("Tell me a joke").is_travel_request is False


def test_nothing_is_invented_when_nothing_was_said():
    extracted = extract_with_rules("Plan a trip to Delhi")
    assert extracted.budget is None
    assert extracted.duration_days is None
    assert extracted.transport_mode is None


# --- turning the extraction into state --------------------------------------


def test_dates_are_computed_in_python_not_guessed():
    state = to_state(ExtractedRequest(destination="Delhi", duration_days=5))
    start = date.fromisoformat(state["start_date"])
    assert date.fromisoformat(state["end_date"]) == start + timedelta(days=4)
    assert state["duration_days"] == 5


def test_a_given_start_date_is_respected():
    state = to_state(
        ExtractedRequest(destination="Delhi", start_date="2026-11-01", duration_days=3)
    )
    assert state["start_date"] == "2026-11-01"
    assert state["end_date"] == "2026-11-03"


def test_preferences_are_only_set_when_stated():
    assert to_state(ExtractedRequest(destination="Delhi"))["preferences"] == {}
    stated = to_state(ExtractedRequest(destination="Delhi", transport_mode="train"))
    assert stated["preferences"] == {"transport_mode": "train"}


def test_invented_interests_are_dropped():
    state = to_state(ExtractedRequest(destination="Delhi", interests=["historical", "teleporting"]))
    assert state["interests"] == ["historical"]


def test_an_invented_transport_mode_is_dropped():
    state = to_state(ExtractedRequest(destination="Delhi", transport_mode="hovercraft"))
    assert state["preferences"] == {}


# --- the node ---------------------------------------------------------------


def test_the_llm_result_is_used_when_a_provider_answers():
    extracted = ExtractedRequest(origin="Kochi", destination="Jaipur", duration_days=4)
    result = LLMResult(data={}, provider="groq", fallback_used=False)

    with patch.object(extraction, "extract_with_llm", return_value=(extracted, result)):
        updates = nodes.parse_request({"user_request": DEMO})

    assert updates["destination"] == "Jaipur"
    assert updates["degraded"] == []


def test_using_the_fallback_provider_is_recorded():
    extracted = ExtractedRequest(origin="Kochi", destination="Delhi", duration_days=5)
    result = LLMResult(data={}, provider="gemini", fallback_used=True)

    with patch.object(extraction, "extract_with_llm", return_value=(extracted, result)):
        updates = nodes.parse_request({"user_request": DEMO})

    assert updates["degraded"] == ["primary LLM unavailable, used gemini"]


def test_no_llm_falls_back_to_rules_and_says_so():
    """A degraded extraction must never look like a normal one."""
    with patch.object(extraction, "extract_with_llm", side_effect=LLMUnavailable("no keys")):
        updates = nodes.parse_request({"user_request": DEMO})

    assert updates["origin"] == "Kochi"
    assert updates["destination"] == "Delhi"
    assert any("simple rules" in note for note in updates["degraded"])


def test_a_non_travel_request_is_rejected_with_a_helpful_message():
    with patch.object(extraction, "extract_with_llm", side_effect=LLMUnavailable("no keys")):
        updates = nodes.parse_request({"user_request": "Tell me a joke"})

    assert updates["status"] == "invalid_request"
    assert "travel planning assistant" in updates["validation_errors"][0]


def test_an_off_topic_rejection_is_not_buried_under_field_errors():
    """validate_request must not overwrite the answer parse_request already gave."""
    assert nodes.validate_request({"status": "invalid_request"}) == {}


def test_requirements_already_in_state_are_not_overwritten():
    """A resumed trip keeps what was extracted the first time."""
    with patch.object(extraction, "extract_with_llm", side_effect=LLMUnavailable("no keys")):
        updates = nodes.parse_request(
            {"user_request": DEMO, "destination": "Goa", "budget": 9000.0}
        )

    assert "destination" not in updates
    assert "budget" not in updates
