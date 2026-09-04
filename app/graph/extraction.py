"""Turning a traveller's message into structured requirements.

Two routes to the same shape:

- extract_with_llm: the normal path, structured output with a fallback provider.
- extract_with_rules: a keyword and regex reader used when no LLM can be
  reached, so the system still plans a trip with no API keys configured. It is
  deliberately dumber, and the workflow records that it was used.
"""

import logging
import re
from datetime import date, timedelta

from pydantic import BaseModel, Field

from app.graph.prompts import EXTRACT_REQUEST_SYSTEM, EXTRACT_REQUEST_USER
from app.llm.client import LLMResult, call_llm
from mcp_servers.cities import CITIES

logger = logging.getLogger(__name__)

VALID_INTERESTS = {"historical", "nature", "food", "culture", "adventure", "shopping"}
VALID_MODES = {"flight", "train", "bus"}

# How far ahead to plan when the traveller gives no dates.
DEFAULT_LEAD_DAYS = 14
DEFAULT_DURATION_DAYS = 5
DEFAULT_TRAVELERS = 1


class ExtractedRequest(BaseModel):
    """What we try to read out of the traveller's message."""

    is_travel_request: bool = True
    origin: str | None = None
    destination: str | None = None
    start_date: str | None = None
    duration_days: int | None = None
    travelers: int | None = None
    budget: float | None = None
    transport_mode: str | None = None
    max_journey_hours: float | None = None
    interests: list[str] = Field(default_factory=list)


def extract_with_llm(message: str) -> tuple[ExtractedRequest, LLMResult]:
    """Ask the LLM. Raises LLMUnavailable if no provider answers."""
    return call_llm(
        EXTRACT_REQUEST_SYSTEM,
        EXTRACT_REQUEST_USER.format(message=message),
        ExtractedRequest,
    )


def extract_with_rules(message: str) -> ExtractedRequest:
    """Read what we can with plain Python. Used only when no LLM is reachable."""
    text = message.lower()

    origin, destination = _find_cities(message)
    return ExtractedRequest(
        is_travel_request=_looks_like_travel(text) or bool(destination),
        origin=origin,
        destination=destination,
        duration_days=_find_duration(text),
        travelers=_find_travelers(text),
        budget=_find_budget(text),
        transport_mode=_find_mode(text),
        max_journey_hours=_find_hour_limit(text),
        interests=sorted(word for word in VALID_INTERESTS if word in text),
    )


def to_state(extracted: ExtractedRequest) -> dict:
    """Fill in the defaults and compute the dates. Dates are always Python's job."""
    duration = extracted.duration_days or DEFAULT_DURATION_DAYS
    start = (
        date.fromisoformat(extracted.start_date)
        if extracted.start_date
        else date.today() + timedelta(days=DEFAULT_LEAD_DAYS)
    )

    preferences = {}
    if extracted.transport_mode in VALID_MODES:
        preferences["transport_mode"] = extracted.transport_mode
    if extracted.max_journey_hours:
        preferences["max_journey_hours"] = extracted.max_journey_hours

    return {
        "origin": extracted.origin,
        "destination": extracted.destination,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=duration - 1)).isoformat(),
        "duration_days": duration,
        "travelers": extracted.travelers or DEFAULT_TRAVELERS,
        "budget": extracted.budget,
        "preferences": preferences,
        "interests": [i for i in extracted.interests if i in VALID_INTERESTS],
    }


# --- the rule-based reader --------------------------------------------------

TRAVEL_WORDS = ("trip", "travel", "visit", "holiday", "vacation", "itinerary", "tour", "plan")


def _looks_like_travel(text: str) -> bool:
    return any(word in text for word in TRAVEL_WORDS)


def _find_cities(message: str) -> tuple[str | None, str | None]:
    """Read the origin and destination.

    "from X" and "to X" are looked for independently, so "to Kochi, leaving from
    Delhi" reads the same way as "from Delhi to Kochi". Only when neither marker
    is present do we fall back to the order the cities appear in.
    """
    names = {city.lower(): city for city in CITIES}
    known = "|".join(sorted(names, key=len, reverse=True))

    origin = _match_marker(message, rf"\bfrom\s+({known})\b", names)
    destination = _match_marker(message, rf"\bto\s+({known})\b", names)
    if origin and destination:
        return origin, destination

    mentioned = []
    for city in CITIES:
        position = message.lower().find(city.lower())
        if position >= 0:
            mentioned.append((position, city))
    mentioned.sort()
    remaining = [city for _, city in mentioned if city not in {origin, destination}]

    if origin and not destination:
        return origin, remaining[0] if remaining else None
    if destination and not origin:
        return (remaining[0] if remaining else None), destination
    if len(remaining) >= 2:
        return remaining[0], remaining[1]
    if len(remaining) == 1:
        return None, remaining[0]
    return None, None


def _match_marker(message: str, pattern: str, names: dict[str, str]) -> str | None:
    match = re.search(pattern, message, re.IGNORECASE)
    return names.get(match.group(1).lower()) if match else None


def _find_duration(text: str) -> int | None:
    match = re.search(r"(\d+)[\s-]*(?:day|night)", text)
    return int(match.group(1)) if match else None


def _find_travelers(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:people|persons?|travell?ers?|adults?|pax|guests?)", text)
    if match:
        return int(match.group(1))
    # A bare "for 2", but not "for 5 days".
    bare = re.search(r"\bfor\s+(\d+)\b(?!\s*(?:day|night|hour|hr))", text)
    if bare:
        return int(bare.group(1))
    if "couple" in text:
        return 2
    if "solo" in text or "alone" in text:
        return 1
    return None


def _find_budget(text: str) -> float | None:
    """Handles "50000", "50,000", "rs 50k" and "1.5 lakh"."""
    lakh = re.search(r"([\d.]+)\s*lakh", text)
    if lakh:
        return float(lakh.group(1)) * 100_000

    # The trailing lookahead keeps "under 12 hours" and "for 5 days" from being
    # read as amounts of money.
    match = re.search(
        r"(?:budget|under|below|within|rs\.?|inr|₹)\D{0,4}([\d,]*\d)(?![\d,])\s*(k\b|thousand\b)?"
        r"(?!\s*(?:hour|hr|day|night|people|person|pax|am|pm))",
        text,
    )
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    return amount * 1000 if match.group(2) else amount


def _find_mode(text: str) -> str | None:
    for mode, words in (
        ("train", ("train", "rail")),
        ("flight", ("flight", "fly", "plane", "air")),
        ("bus", ("bus",)),
    ):
        if any(f"prefer {word}" in text or f"by {word}" in text for word in words):
            return mode
    return None


def _find_hour_limit(text: str) -> float | None:
    match = re.search(r"(?:under|less than|below|within|<)\s*(\d+)\s*(?:hour|hr)", text)
    return float(match.group(1)) if match else None
