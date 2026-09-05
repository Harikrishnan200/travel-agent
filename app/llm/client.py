"""Calling an LLM, with a fallback provider.

    primary (Groq) --fails--> fallback (Gemini) --fails--> LLMUnavailable

That is the whole design. No load balancing, no model scoring, no cost
optimiser: the only question this answers is "the primary is down, can we keep
going?".

Every call asks for structured output and parses it into a Pydantic model, so a
malformed response is treated exactly like a failed call - and falls back too.
"""

import json
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


class LLMUnavailable(RuntimeError):
    """No provider could answer. The caller decides whether to degrade or fail."""


class LLMResult(BaseModel):
    """What came back, and which provider produced it."""

    data: dict
    provider: str
    fallback_used: bool


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
            try:
                raw = _invoke(name, system_prompt, user_prompt, response_model)
                parsed = response_model.model_validate(raw)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                metrics.llm_calls.labels(provider=name, status="error").inc()
                logger.warning(
                    "llm_call_failed", provider=name, attempt=attempt + 1, error=str(exc)
                )
                continue

            metrics.llm_calls.labels(provider=name, status="ok").inc()
            if index > 0:
                logger.warning("llm_fallback_used", provider=name)
            return parsed, LLMResult(
                data=parsed.model_dump(), provider=name, fallback_used=index > 0
            )

    raise LLMUnavailable("All LLM providers failed: " + " | ".join(errors))


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
    schema = json.dumps(model.model_json_schema(), indent=2)
    instructions = f"{system_prompt}\n\nReply with JSON matching this schema:\n{schema}"
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
