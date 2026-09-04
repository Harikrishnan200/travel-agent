"""Accommodation Agent: pick a hotel that fits the budget the manager allocated.

Tool it may call: search_hotels. Nothing else.
"""

import logging

from app.agents.schemas import AccommodationResult, Hotel

logger = logging.getLogger(__name__)

# Below this rating we would rather report "nothing suitable" than recommend it.
MIN_ACCEPTABLE_RATING = 3.0


def research_accommodation(brief: dict, tools: dict) -> AccommodationResult:
    """Search hotels in the destination and recommend the best one within budget."""
    try:
        raw = tools["search_hotels"](
            city=brief["city"],
            check_in=brief["check_in"],
            check_out=brief["check_out"],
            guests=brief["travelers"],
            budget=None,  # filter in the agent so we can explain what was excluded
        )
    except Exception as exc:
        logger.warning("hotel search failed: error=%s", exc)
        return AccommodationResult(
            reason="Hotel search is unavailable.", failures=["hotel search unavailable"]
        )

    hotels = [Hotel(**item) for item in raw if item["rating"] >= MIN_ACCEPTABLE_RATING]
    if not hotels:
        return AccommodationResult(reason="No hotels were found for these dates.")

    max_total = brief.get("max_total_price")
    affordable = [h for h in hotels if max_total is None or h.total_price <= max_total]

    if affordable:
        # Best rating we can afford, cheapest among equally rated.
        recommended = max(affordable, key=lambda h: (h.rating, -h.price_per_night))
        reason = (
            f"{recommended.name} in {recommended.location} is rated {recommended.rating} "
            f"and costs Rs {recommended.total_price:,.0f} for {recommended.nights} nights"
        )
        if max_total is not None:
            reason += f", within the Rs {max_total:,.0f} accommodation budget."
        else:
            reason += "."
    else:
        # Nothing fits: recommend the cheapest and let the validator flag the overrun.
        recommended = min(hotels, key=lambda h: h.total_price)
        reason = (
            f"No hotel fits the Rs {max_total:,.0f} accommodation budget. "
            f"{recommended.name} is the cheapest at Rs {recommended.total_price:,.0f}."
        )

    alternatives = sorted(
        (h for h in hotels if h.name != recommended.name), key=lambda h: h.total_price
    )[:5]
    return AccommodationResult(recommended=recommended, alternatives=alternatives, reason=reason)
