"""Output guardrail: the last deterministic check before a plan leaves the system.

It re-derives the numbers rather than trusting the ones in the state. The point
is to catch a total that drifted - whether from a bug, a re-plan that updated
one field and not another, or an LLM that was allowed near an arithmetic.
"""

import re

# Anything that looks like a credential must never reach the traveller.
SECRET_PATTERNS = [
    r"\bsk-[A-Za-z0-9]{16,}",
    r"\bgsk_[A-Za-z0-9]{16,}",
    r"\bAIza[A-Za-z0-9_\-]{20,}",
    r"api[_ ]?key\s*[:=]\s*\S+",
]

REQUIRED_PLAN_FIELDS = ("origin", "destination", "start_date", "end_date", "travelers")


def check_plan(state: dict, recomputed_total: float) -> list[str]:
    """Verify the finished plan. Returns the problems found, empty if it is sound."""
    errors = []

    for field in REQUIRED_PLAN_FIELDS:
        if not state.get(field):
            errors.append(f"The final plan is missing {field.replace('_', ' ')}.")

    stated = state.get("total_cost")
    if stated is None:
        errors.append("The final plan has no total cost.")
    elif abs(stated - recomputed_total) > 0.01:
        errors.append(
            f"Total cost does not add up: the plan says Rs {stated:,.0f} but the "
            f"parts come to Rs {recomputed_total:,.0f}."
        )

    breakdown = state.get("cost_breakdown") or {}
    if breakdown and abs(sum(breakdown.values()) - recomputed_total) > 0.01:
        errors.append("The cost breakdown does not sum to the total.")

    budget = state.get("budget")
    if budget is not None and recomputed_total > budget:
        errors.append(
            f"The approved plan costs Rs {recomputed_total:,.0f}, over the "
            f"Rs {budget:,.0f} budget."
        )

    errors.extend(check_response_text(state.get("final_response") or ""))
    return errors


def check_response_text(text: str) -> list[str]:
    """No credentials in what we send back."""
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            return ["The response contained something that looked like a credential."]
    return []
