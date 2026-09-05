"""Turning a traveller's reply into changes we can actually make.

Same shape as request extraction: the LLM reads it, and a keyword reader takes
over when no provider answers, so a modification still does something with no
API keys configured.
"""

import logging
import re

from pydantic import BaseModel, Field

from app.graph.prompts import INTERPRET_FEEDBACK_SYSTEM, INTERPRET_FEEDBACK_USER
from app.llm.client import call_llm

logger = logging.getLogger(__name__)

VALID_INTERESTS = {"historical", "nature", "food", "culture", "adventure", "shopping"}


class FeedbackIntent(BaseModel):
    """What the traveller wants changed."""

    cheaper_hotel: bool = False
    cheaper_transport: bool = False
    fewer_paid_activities: bool = False
    add_interests: list[str] = Field(default_factory=list)
    new_budget: float | None = None


def interpret_with_llm(feedback: str) -> tuple[FeedbackIntent, object]:
    return call_llm(
        INTERPRET_FEEDBACK_SYSTEM,
        INTERPRET_FEEDBACK_USER.format(feedback=feedback),
        FeedbackIntent,
    )


def interpret_with_rules(feedback: str) -> FeedbackIntent:
    """Read the feedback with plain keywords. Used when no LLM is reachable."""
    text = feedback.lower()
    cheaper = any(
        word in text for word in ("cheaper", "reduce", "lower", "less expensive", "save", "budget")
    )

    return FeedbackIntent(
        cheaper_hotel=cheaper and _mentions(text, ("hotel", "stay", "accommodation", "room")),
        cheaper_transport=cheaper and _mentions(text, ("flight", "train", "bus", "travel", "fare")),
        fewer_paid_activities=cheaper
        and _mentions(text, ("activity", "activities", "sightseeing", "ticket")),
        add_interests=sorted(
            interest
            for interest in VALID_INTERESTS
            if interest in text and _mentions(text, ("more", "add", "extra", "include"))
        ),
        new_budget=_find_budget(text),
    )


def _mentions(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _find_budget(text: str) -> float | None:
    """Only an explicitly raised or set budget, e.g. "increase the budget to 70000"."""
    match = re.search(r"budget\D{0,12}?([\d,]*\d)(?![\d,])\s*(k\b|thousand\b|lakh\b)?", text)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    unit = match.group(2)
    if unit == "lakh":
        return amount * 100_000
    return amount * 1000 if unit else amount
