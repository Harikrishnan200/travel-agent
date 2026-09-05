"""Input guardrails.

Two checks, run at different moments:

- check_message: on the raw text, before it reaches the LLM. Catches things that
  should never be sent to a model at all.
- check_requirements: on the extracted fields, before the agents start work.
  Catches a request we understood but cannot plan.

Plain Python and a short list of patterns. There is no guardrail framework here
because there is nothing a framework would add.
"""

import re

from app.config import get_settings

settings = get_settings()

MAX_MESSAGE_LENGTH = 2000

OFF_TOPIC_REPLY = (
    "I am a travel planning assistant. Please describe the trip you would like to "
    "plan, including where you are travelling from and to."
)

# Attempts to talk to the system rather than to the travel planner. This is a
# speed bump, not a wall: the real defences are the restricted tool access and
# the deterministic validation that follow.
INJECTION_PATTERNS = [
    r"ignore (?:all |the |your )?(?:previous|prior|above) instructions",
    r"disregard (?:all |the |your )?(?:previous|prior|above)",
    r"you are now (?:a|an|no longer)",
    r"system prompt",
    r"reveal (?:your |the )?(?:prompt|instructions|api[_ ]?key)",
    r"</?(?:system|instructions)>",
]


class InputRejected(Exception):
    """The message will not be planned. `reply` is what the traveller is told."""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.reply = reply


def check_message(message: str) -> None:
    """Screen the raw message. Raises InputRejected if it should not be planned."""
    if not message or not message.strip():
        raise InputRejected("Please describe the trip you would like to plan.")

    if len(message) > MAX_MESSAGE_LENGTH:
        raise InputRejected(
            f"That message is too long ({len(message)} characters). "
            f"Please keep it under {MAX_MESSAGE_LENGTH}."
        )

    lowered = message.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            raise InputRejected(OFF_TOPIC_REPLY)


def check_requirements(state: dict) -> list[str]:
    """Check the extracted fields. Returns the reasons we cannot plan this trip."""
    errors = []

    if not state.get("origin"):
        errors.append("I could not tell which city you are travelling from.")
    if not state.get("destination"):
        errors.append("I could not tell which city you are travelling to.")
    if state.get("origin") and state.get("origin") == state.get("destination"):
        errors.append("The origin and destination are the same city.")

    duration = state.get("duration_days") or 0
    if not 1 <= duration <= settings.max_trip_days:
        errors.append(f"Trip length must be between 1 and {settings.max_trip_days} days.")

    travelers = state.get("travelers") or 0
    if not 1 <= travelers <= settings.max_travelers:
        errors.append(f"Traveller count must be between 1 and {settings.max_travelers}.")

    # A budget is optional; a nonsensical one is not.
    budget = state.get("budget")
    if budget is not None and budget <= 0:
        errors.append("Budget must be a positive amount.")

    # Dates are computed in Python, so a broken range here means a bad extraction.
    from app.validation.validators import validate_dates

    errors.extend(validate_dates(state))

    return errors
