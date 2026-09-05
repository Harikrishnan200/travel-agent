"""LLM fallback: primary fails, fallback answers, and what happens when neither does.

No network: each provider's invoke function is replaced, so the chain logic is
what is under test.
"""

import json

import pytest
from pydantic import BaseModel

from app.llm import client
from app.llm.client import LLMUnavailable, call_llm


class Answer(BaseModel):
    city: str
    days: int


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Skip the retry backoff. The delay is real, but waiting for it is not a test."""
    monkeypatch.setattr(client, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(client, "MAX_BACKOFF_SECONDS", 0.0)


@pytest.fixture
def both_providers_configured(monkeypatch):
    monkeypatch.setattr(client.settings, "groq_api_key", "test-groq-key")
    monkeypatch.setattr(client.settings, "gemini_api_key", "test-gemini-key")
    monkeypatch.setattr(client.settings, "primary_llm_provider", "groq")
    monkeypatch.setattr(client.settings, "fallback_llm_provider", "gemini")


def fake_invoke(monkeypatch, behaviour):
    """Replace the provider call with a function driven by a {provider: result} map."""
    calls = []

    def invoke(provider, system_prompt, user_prompt, model):
        calls.append(provider)
        outcome = behaviour[provider]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client, "_invoke", invoke)
    return calls


# --- the happy path ---------------------------------------------------------


def test_the_primary_answers_and_no_fallback_is_used(monkeypatch, both_providers_configured):
    calls = fake_invoke(monkeypatch, {"groq": {"city": "Delhi", "days": 5}})

    answer, result = call_llm("system", "user", Answer)

    assert answer.city == "Delhi"
    assert result.provider == "groq"
    assert result.fallback_used is False
    assert calls == ["groq"]


# --- falling back -----------------------------------------------------------


def test_the_primary_is_retried_before_falling_back(monkeypatch, both_providers_configured):
    """A transient blip should not cost us the primary provider."""
    calls = fake_invoke(
        monkeypatch,
        {"groq": TimeoutError("timeout"), "gemini": {"city": "Jaipur", "days": 3}},
    )

    _answer, result = call_llm("system", "user", Answer)

    assert calls == ["groq", "groq", "gemini"]
    assert result.provider == "gemini"
    assert result.fallback_used is True


def test_a_malformed_response_is_treated_like_a_failure(monkeypatch, both_providers_configured):
    """A reply that does not match the schema is no more useful than no reply."""
    calls = fake_invoke(
        monkeypatch,
        {"groq": {"city": "Delhi"}, "gemini": {"city": "Delhi", "days": 5}},
    )

    _answer, result = call_llm("system", "user", Answer)

    assert result.provider == "gemini"
    assert calls == ["groq", "groq", "gemini"]


def test_the_fallback_is_not_retried(monkeypatch, both_providers_configured):
    """One attempt each on the way down; the workflow should not stall."""
    calls = fake_invoke(
        monkeypatch, {"groq": TimeoutError("down"), "gemini": TimeoutError("also down")}
    )

    with pytest.raises(LLMUnavailable):
        call_llm("system", "user", Answer)

    assert calls == ["groq", "groq", "gemini"]


# --- when nothing answers ---------------------------------------------------


def test_both_providers_failing_raises_with_both_reasons(monkeypatch, both_providers_configured):
    fake_invoke(
        monkeypatch,
        {"groq": TimeoutError("groq timed out"), "gemini": ValueError("gemini rejected the key")},
    )

    with pytest.raises(LLMUnavailable) as error:
        call_llm("system", "user", Answer)

    assert "groq timed out" in str(error.value)
    assert "gemini rejected the key" in str(error.value)


def test_no_api_keys_configured_raises_a_clear_error(monkeypatch):
    monkeypatch.setattr(client.settings, "groq_api_key", "")
    monkeypatch.setattr(client.settings, "gemini_api_key", "")

    with pytest.raises(LLMUnavailable, match="No LLM provider is configured"):
        call_llm("system", "user", Answer)


# --- provider chain ---------------------------------------------------------


def test_a_provider_with_no_key_is_skipped(monkeypatch):
    monkeypatch.setattr(client.settings, "groq_api_key", "")
    monkeypatch.setattr(client.settings, "gemini_api_key", "key")
    monkeypatch.setattr(client.settings, "primary_llm_provider", "groq")
    monkeypatch.setattr(client.settings, "fallback_llm_provider", "gemini")

    assert client._provider_chain() == ["gemini"]


def test_the_same_provider_is_not_tried_twice(monkeypatch):
    monkeypatch.setattr(client.settings, "groq_api_key", "key")
    monkeypatch.setattr(client.settings, "primary_llm_provider", "groq")
    monkeypatch.setattr(client.settings, "fallback_llm_provider", "groq")

    assert client._provider_chain() == ["groq"]


def test_the_configured_order_is_respected(monkeypatch):
    monkeypatch.setattr(client.settings, "groq_api_key", "key")
    monkeypatch.setattr(client.settings, "gemini_api_key", "key")
    monkeypatch.setattr(client.settings, "primary_llm_provider", "gemini")
    monkeypatch.setattr(client.settings, "fallback_llm_provider", "groq")

    assert client._provider_chain() == ["gemini", "groq"]


def test_an_unknown_provider_name_is_rejected():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        client._invoke("cohere", "system", "user", Answer)


# --- provider request shapes -------------------------------------------------
# These cannot be exercised against the real APIs without keys, so they check
# that each SDK is called correctly: right model, JSON mode, deterministic.


def test_groq_is_asked_for_json_at_temperature_zero(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = type("M", (), {"content": '{"city": "Delhi", "days": 5}'})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    class FakeGroq:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(client.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(client.settings, "groq_model", "llama-3.3-70b-versatile")
    monkeypatch.setitem(__import__("sys").modules, "groq", type("M", (), {"Groq": FakeGroq}))

    result = client._invoke_groq("be helpful", "plan a trip", Answer)

    assert result == {"city": "Delhi", "days": 5}
    assert captured["model"] == "llama-3.3-70b-versatile"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == 0
    assert captured["client_kwargs"]["api_key"] == "test-key"


def test_the_groq_system_prompt_carries_the_schema(monkeypatch):
    """Groq's JSON mode needs the shape spelled out, or it invents one."""
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = type("M", (), {"content": "{}"})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    class FakeGroq:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(client.settings, "groq_api_key", "k")
    monkeypatch.setitem(__import__("sys").modules, "groq", type("M", (), {"Groq": FakeGroq}))

    client._invoke_groq("be helpful", "plan a trip", Answer)

    system = captured["messages"][0]["content"]
    assert "be helpful" in system
    assert '"city"' in system and '"days"' in system
    assert captured["messages"][1]["content"] == "plan a trip"


# --- backing off ------------------------------------------------------------


@pytest.fixture
def real_backoff(monkeypatch):
    """Undo the autouse fixture that skips waiting, so the maths can be checked."""
    monkeypatch.setattr(client, "RETRY_BACKOFF_SECONDS", 5.0)
    monkeypatch.setattr(client, "MAX_BACKOFF_SECONDS", 20.0)


def test_a_rate_limit_hint_is_obeyed(real_backoff):
    """Retrying instantly after "try again in 7.8s" is guaranteed to fail again."""
    error = "Rate limit reached. Please try again in 7.7775s. Need more tokens?"
    assert client._backoff_for(error) == pytest.approx(8.2775)


def test_a_very_long_hint_is_capped(real_backoff):
    assert client._backoff_for("try again in 600s") == 20.0


def test_an_error_with_no_hint_uses_the_default_wait(real_backoff):
    assert client._backoff_for("connection reset") == 5.0


def test_the_prompt_schema_is_compact():
    """The full JSON schema costs a thousand tokens and says nothing extra."""
    from app.graph.extraction import ExtractedRequest

    lean = client._schema_for(ExtractedRequest)
    full = json.dumps(ExtractedRequest.model_json_schema())
    assert len(lean) < len(full) / 3
    for field in ExtractedRequest.model_fields:
        assert field in lean


def test_optional_and_list_types_survive_the_trim():
    from app.graph.extraction import ExtractedRequest

    lean = json.loads(client._schema_for(ExtractedRequest))
    assert lean["origin"] == "string or null"
    assert lean["interests"] == "array of string"
    assert lean["is_travel_request"] == "boolean"
