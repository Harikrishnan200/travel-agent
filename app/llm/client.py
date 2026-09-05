"""Calling an LLM, with a fallback provider.

    primary (Groq) --fails--> fallback (Gemini) --fails--> LLMUnavailable

That is the whole design. No load balancing, no model scoring, no cost
optimiser: the only question this answers is "the primary is down, can we keep
going?".

Every call asks for structured output and parses it into a Pydantic model, so a
malformed response is treated exactly like a failed call - and falls back too.
"""

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar

from pydantic import BaseModel

from app import metrics
from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)
settings = get_settings()

Model = TypeVar("Model", bound=BaseModel)

# One retry on the primary covers a transient blip without stalling the workflow.
PRIMARY_ATTEMPTS = 2

# Wait before retrying, but only as long as the failure warrants. A rate limit
# is measured per minute, so retrying instantly is guaranteed to fail again -
# those wait, honouring the provider's own "try again in Ns" hint. Anything else
# (a timeout, a dropped connection) gets a token pause, because several LLM
# calls happen per request and a five second wait on each one turns a slow
# response into a minute-long one.
RATE_LIMIT_BACKOFF_SECONDS = 5.0
TRANSIENT_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 20.0
RETRY_HINT = re.compile(r"try again in ([\d.]+)s")
RATE_LIMITED = re.compile(r"rate.?limit|429|quota|too many requests", re.IGNORECASE)


class LLMUnavailable(RuntimeError):
    """No provider could answer. The caller decides whether to degrade or fail."""


class LLMResult(BaseModel):
    """What came back, and which provider produced it."""

    data: dict
    provider: str
    fallback_used: bool


# Which providers answered during the current request. A context variable rather
# than workflow state, because the question being answered is "what did this API
# call use?" - and a resumed trip has already used an LLM on an earlier call.
_usage: ContextVar[list[dict] | None] = ContextVar("llm_usage", default=None)


@contextmanager
def track_usage():
    """Collect which providers answer inside this block."""
    token = _usage.set([])
    try:
        yield lambda: list(_usage.get() or [])
    finally:
        _usage.reset(token)


def _record(provider: str, status: str, duration_ms: float, fallback_used: bool) -> None:
    calls = _usage.get()
    if calls is not None:
        calls.append(
            {
                "provider": provider,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "fallback_used": fallback_used,
            }
        )


def summarize_usage(calls: list[dict]) -> dict | None:
    """One line about the LLM behind a response, for the API to return."""
    if not calls:
        return None

    answered = [call for call in calls if call["status"] == "ok"]
    return {
        # The provider that actually produced the answers, if only one did.
        "provider": answered[-1]["provider"] if answered else None,
        "providers_tried": list(dict.fromkeys(call["provider"] for call in calls)),
        "fallback_used": any(call["fallback_used"] for call in answered),
        "calls": len(calls),
        "failed_calls": len(calls) - len(answered),
        "total_duration_ms": round(sum(call["duration_ms"] for call in calls), 1),
    }


def call_llm(
    system_prompt: str,
    user_prompt: str,
    response_model: type[Model],
) -> tuple[Model, LLMResult]:
    """Ask the LLM for a structured answer, falling back if the primary fails."""
    providers = _provider_chain()
    if not providers:
        raise LLMUnavailable("No LLM provider is configured. Set GROQ_API_KEY or GEMINI_API_KEY.")

    errors = []
    for index, name in enumerate(providers):
        attempts = PRIMARY_ATTEMPTS if index == 0 else 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                raw = _invoke(name, system_prompt, user_prompt, response_model)
                parsed = response_model.model_validate(raw)
            except Exception as exc:
                elapsed = (time.monotonic() - started) * 1000
                errors.append(f"{name}: {exc}")
                metrics.llm_calls.labels(provider=name, status="error").inc()
                _record(name, "error", elapsed, fallback_used=index > 0)
                logger.warning(
                    "llm_call_failed", provider=name, attempt=attempt + 1, error=str(exc)
                )
                if attempt + 1 < attempts:
                    time.sleep(_backoff_for(str(exc)))
                continue

            metrics.llm_calls.labels(provider=name, status="ok").inc()
            _record(name, "ok", (time.monotonic() - started) * 1000, fallback_used=index > 0)
            if index > 0:
                logger.warning("llm_fallback_used", provider=name)
            return parsed, LLMResult(
                data=parsed.model_dump(), provider=name, fallback_used=index > 0
            )

    raise LLMUnavailable("All LLM providers failed: " + " | ".join(errors))


def _backoff_for(error: str) -> float:
    """How long to wait before trying again, based on why the call failed."""
    hint = RETRY_HINT.search(error)
    if hint:
        return min(float(hint.group(1)) + 0.5, MAX_BACKOFF_SECONDS)
    if RATE_LIMITED.search(error):
        return RATE_LIMIT_BACKOFF_SECONDS
    return TRANSIENT_BACKOFF_SECONDS


def _schema_for(model: type[BaseModel]) -> str:
    """A compact schema for the prompt.

    The full JSON schema carries titles and descriptions that cost a thousand
    tokens and tell the model nothing it cannot see from the field names. On a
    free tier metered in tokens per minute, that is the difference between
    working and being rate limited.

    Nested models are described in full, not flattened to "object": a model told
    only that it needs "an array of object" answers with an array of strings.
    """
    schema = model.model_json_schema()
    return json.dumps(_describe_object(schema, schema.get("$defs", {})), separators=(",", ":"))


def _describe_object(spec: dict, defs: dict) -> dict:
    return {name: _describe_type(field, defs) for name, field in spec.get("properties", {}).items()}


def _describe_type(spec: dict, defs: dict, depth: int = 0):
    """One field's type, as either a type name or a nested shape."""
    if "$ref" in spec and depth < 4:
        target = defs.get(spec["$ref"].rsplit("/", 1)[-1], {})
        return _describe_object(target, defs)

    if "anyOf" in spec:
        options = [_describe_type(option, defs, depth + 1) for option in spec["anyOf"]]
        objects = [option for option in options if isinstance(option, dict)]
        if objects:
            return objects[0]
        return " or ".join(dict.fromkeys(options))

    kind = spec.get("type", "any")
    if kind == "array":
        items = _describe_type(spec.get("items", {}), defs, depth + 1)
        return [items] if isinstance(items, dict) else f"array of {items}"
    return kind


def _provider_chain() -> list[str]:
    """Primary first, then fallback, skipping any provider with no API key."""
    chain = [settings.primary_llm_provider, settings.fallback_llm_provider]
    seen, configured = set(), []
    for name in chain:
        if name in seen or not _api_key(name):
            continue
        seen.add(name)
        configured.append(name)
    return configured


def _api_key(provider: str) -> str:
    return {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key}.get(provider, "")


def _invoke(provider: str, system_prompt: str, user_prompt: str, model: type[BaseModel]) -> dict:
    """Call one provider and return the parsed JSON object it produced."""
    if provider == "groq":
        return _invoke_groq(system_prompt, user_prompt, model)
    if provider == "gemini":
        return _invoke_gemini(system_prompt, user_prompt, model)
    raise ValueError(f"Unknown LLM provider {provider!r}")


def _invoke_groq(system_prompt: str, user_prompt: str, model: type[BaseModel]) -> dict:
    from groq import Groq

    client = Groq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_seconds)
    instructions = (
        f"{system_prompt}\n\nReply with a JSON object with exactly these keys "
        f"and types:\n{_schema_for(model)}"
    )
    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(response.choices[0].message.content)


def _invoke_gemini(system_prompt: str, user_prompt: str, model: type[BaseModel]) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=model,
            temperature=0,
        ),
    )
    return json.loads(response.text)
