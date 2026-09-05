"""Transport Agent: choose between flights, trains and buses.

Tools it may call: search_flights, search_trains, search_buses. Nothing else -
the MCP Gateway enforces that from Phase 5.

The choice is deterministic Python, not an LLM judgement call, because the rule
the user gave us ("trains unless the journey is over 12 hours") is arithmetic.
Phase 7 adds an LLM only for the natural-language `reason`.
"""

import logging

from app.agents.schemas import TransportOption, TransportResult

logger = logging.getLogger(__name__)

# One entry per tool this agent is allowed to use.
SEARCHES = {"flight": "search_flights", "train": "search_trains", "bus": "search_buses"}


def research_transport(brief: dict, tools: dict) -> TransportResult:
    """Search every mode, then pick one according to the traveller's preference.

    `brief` is the scoped request from the Travel Manager.
    `tools` maps tool name -> callable, injected so Phase 5 can swap local
    functions for MCP Gateway calls without touching this logic.
    """
    options: list[TransportOption] = []
    failures: list[str] = []

    for mode, tool_name in SEARCHES.items():
        try:
            raw = tools[tool_name](
                origin=brief["origin"],
                destination=brief["destination"],
                date=brief["date"],
                passengers=brief["travelers"],
            )
            options.extend(TransportOption(**item) for item in raw)
        except Exception as exc:
            # One failing search must not lose the other modes.
            logger.warning("transport search failed: mode=%s error=%s", mode, exc)
            failures.append(f"{mode} search unavailable")

    if not options:
        return TransportResult(
            reason="No transport options were found for this route.", failures=failures
        )

    recommended, reason = _choose(options, brief)
    alternatives = _alternatives(options, recommended)
    return TransportResult(
        recommended=recommended, alternatives=alternatives, reason=reason, failures=failures
    )


# How many alternatives to pass on. Enough for the re-planner to have somewhere
# to go, few enough that the manager is not handed the whole search.
MAX_ALTERNATIVES = 6


def _alternatives(
    options: list[TransportOption], recommended: TransportOption
) -> list[TransportOption]:
    """The runners-up, with every mode represented.

    Taking the first few in the order they were collected would drop whole modes
    off the end - buses always came last, so the re-planner could never offer
    one. The cheapest of each mode is kept first, then the list is filled by
    price.
    """
    rest = [option for option in options if option.id != recommended.id]

    cheapest_per_mode = {}
    for option in sorted(rest, key=lambda o: o.price_per_person):
        cheapest_per_mode.setdefault(option.mode, option)

    kept = list(cheapest_per_mode.values())
    for option in sorted(rest, key=lambda o: o.price_per_person):
        if len(kept) >= MAX_ALTERNATIVES:
            break
        if option not in kept:
            kept.append(option)

    return sorted(kept, key=lambda o: o.price_per_person)


def _choose(options: list[TransportOption], brief: dict) -> tuple[TransportOption, str]:
    """Apply the traveller's stated preference, then fall back sensibly."""
    preferred_mode = brief.get("preferred_mode")
    max_hours = brief.get("max_journey_hours")

    if preferred_mode:
        candidates = [o for o in options if o.mode == preferred_mode]
        if max_hours is not None:
            within_limit = [o for o in candidates if o.duration_hours <= max_hours]
            if within_limit:
                best = min(within_limit, key=lambda o: o.price_per_person)
                return best, (
                    f"{best.mode.title()} matches the preferred mode and its "
                    f"{best.duration_hours}h journey is within the {max_hours}h limit."
                )
            if candidates:
                # Preferred mode exists but is too slow: say so explicitly.
                slowest_ok = min(candidates, key=lambda o: o.duration_hours)
                faster = [o for o in options if o.duration_hours <= max_hours]
                if faster:
                    best = min(faster, key=lambda o: o.price_per_person)
                    return best, (
                        f"The fastest {preferred_mode} takes {slowest_ok.duration_hours}h, "
                        f"over the {max_hours}h limit, so a {best.mode} was chosen instead."
                    )
        elif candidates:
            best = min(candidates, key=lambda o: o.price_per_person)
            return best, f"{best.mode.title()} matches the preferred mode."

    within_limit = [o for o in options if max_hours is None or o.duration_hours <= max_hours]
    pool = within_limit or options
    best = min(pool, key=lambda o: o.price_per_person)
    note = (
        "" if within_limit else " No option met the journey time limit, so the cheapest was kept."
    )
    return best, f"Cheapest available option at Rs {best.price_per_person:,.0f} per person.{note}"
