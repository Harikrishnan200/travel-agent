"""Run the evaluation dataset and score the results.

    python -m evaluation.evaluate            # everything, mock data, no LLM
    python -m evaluation.evaluate --llm      # use whatever LLM is configured
    python -m evaluation.evaluate --case basic_trip

Each case declares what a correct answer looks like, and each check is a small
function that either passes or explains why it did not. There is no scoring
framework here: a list of checks and a count is exactly as much machinery as
this needs.
"""

import argparse
import json
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app import cache
from app.config import get_settings
from app.graph.graph import build_graph
from app.graph.state import new_state
from app.validation import validators
from gateway import mcp_router, service
from gateway.policy import AGENT_TOOL_ACCESS, READ_TOOLS, ToolNotAllowedError, check_access

DATASET = Path(__file__).parent / "dataset.json"


# --- running one case -------------------------------------------------------


def run_case(case: dict, use_llm: bool) -> dict:
    """Plan the trip the case describes and return the final state."""
    settings = get_settings()
    trip_id = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": trip_id}}
    graph = build_graph(MemorySaver())

    service.reset_rate_limits()
    cache.set_client(None)  # a shared cache would let one case answer another

    working = mcp_router.call_tool
    broken = set(case.get("break_tools", []))

    def maybe_broken(tool, arguments):
        if tool in broken:
            raise mcp_router.MCPUnavailableError(f"{tool} server unreachable")
        return working(tool, arguments)

    with ExitStack() as stack:
        stack.enter_context(patch.object(mcp_router, "call_tool", maybe_broken))
        if case.get("no_llm") or not use_llm:
            stack.enter_context(patch.object(settings, "groq_api_key", ""))
            stack.enter_context(patch.object(settings, "gemini_api_key", ""))

        state = graph.invoke(new_state(trip_id, case["message"]), config=config)
        before = dict(state)

        if case.get("feedback"):
            state = graph.invoke(
                Command(resume={"action": "modify", "feedback": case["feedback"]}), config=config
            )
        if case.get("approve"):
            state = graph.invoke(Command(resume={"action": "approve"}), config=config)

    return {"state": state, "before": before, "pending": _pending(graph, config)}


def _pending(graph, config) -> dict | None:
    for task in graph.get_state(config).tasks:
        for interrupt in task.interrupts:
            return interrupt.value
    return None


# --- the checks -------------------------------------------------------------


def check(case: dict, outcome: dict) -> list[str]:
    """Return the reasons this case failed. Empty means it passed."""
    expect = case["expect"]
    state = outcome["state"]
    failures = []

    def fail(what):
        failures.append(what)

    if "status" in expect and state.get("status") != expect["status"]:
        # The errors say why, which is the part worth reading.
        why = "; ".join(state.get("validation_errors", [])) or "no reason recorded"
        fail(f"status was {state.get('status')!r}, expected {expect['status']!r} ({why})")

    for field, wanted in expect.get("extraction", {}).items():
        if state.get(field) != wanted:
            fail(f"{field} was {state.get(field)!r}, expected {wanted!r}")

    if expect.get("within_budget"):
        total, budget = state.get("total_cost"), state.get("budget")
        if budget is not None and total is not None and total > budget:
            fail(f"total {total:,.0f} is over the {budget:,.0f} budget")

    if "itinerary_days" in expect:
        days = len((state.get("itinerary") or {}).get("days", []))
        if days != expect["itinerary_days"]:
            fail(f"itinerary had {days} days, expected {expect['itinerary_days']}")

    if expect.get("no_validation_errors") and state.get("validation_errors"):
        fail(f"validation errors remained: {state['validation_errors']}")

    if "transport_mode" in expect:
        mode = (state.get("selected_transport") or {}).get("mode")
        if mode != expect["transport_mode"]:
            fail(f"chose {mode!r}, expected {expect['transport_mode']!r}")

    if "reason_mentions" in expect:
        reason = (state.get("selected_transport") or {}).get("reason", "")
        if expect["reason_mentions"] not in reason:
            fail(f"reason did not mention {expect['reason_mentions']!r}: {reason!r}")

    if expect.get("bus_searched"):
        options = state.get("transport_options") or {}
        modes = {option.get("mode") for option in options.get("alternatives", [])}
        modes.add((state.get("selected_transport") or {}).get("mode"))
        if "bus" not in modes:
            fail("no bus option appeared for a short route")

    if expect.get("no_fixable_weather_conflicts") and validators.validate_weather(state):
        fail(f"weather conflicts remained: {validators.validate_weather(state)}")

    if "error_mentions" in expect:
        text = " ".join(state.get("validation_errors", [])) + (state.get("final_response") or "")
        if expect["error_mentions"] not in text:
            fail(f"no error mentioned {expect['error_mentions']!r}")

    if "degraded_mentions" in expect:
        if not any(expect["degraded_mentions"] in note for note in state.get("degraded", [])):
            fail(
                f"degraded did not mention {expect['degraded_mentions']!r}: "
                f"{state.get('degraded')}"
            )

    if "offers_options" in expect:
        offered = (outcome["pending"] or {}).get("options")
        if offered != expect["offers_options"]:
            fail(f"offered {offered!r}, expected {expect['offers_options']!r}")

    if expect.get("has_total_cost") and not state.get("total_cost"):
        fail("no total cost was calculated")

    if expect.get("has_final_response") and not state.get("final_response"):
        fail("no final response was produced")

    if expect.get("hotel_got_cheaper"):
        before = (outcome["before"].get("selected_hotel") or {}).get("total_price", 0)
        after = (state.get("selected_hotel") or {}).get("total_price", 0)
        if after >= before:
            fail(f"hotel went from {before:,.0f} to {after:,.0f}, expected cheaper")

    if "added_interest" in expect:
        if expect["added_interest"] not in (state.get("interests") or []):
            fail(f"{expect['added_interest']!r} was not added to the interests")

    if expect.get("cross_domain_calls_refused"):
        failures.extend(_authorization_failures())

    return failures


def _authorization_failures() -> list[str]:
    """Every agent must be refused every tool outside its own domain."""
    problems = []
    for agent, allowed in AGENT_TOOL_ACCESS.items():
        for tool in READ_TOOLS - allowed:
            try:
                check_access(agent, tool)
            except ToolNotAllowedError:
                continue
            problems.append(f"{agent} was allowed {tool}")
    return problems


# --- reporting --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="use the configured LLM")
    parser.add_argument("--case", help="run one case by id")
    parser.add_argument("--verbose", action="store_true", help="show why each case failed")
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"No case called {args.case!r}")
            return 2

    print(f"Running {len(cases)} cases ({'with' if args.llm else 'without'} an LLM)\n")
    results = []

    for case in cases:
        try:
            outcome = run_case(case, use_llm=args.llm)
            failures = check(case, outcome)
        except Exception as exc:
            failures = [f"raised {type(exc).__name__}: {exc}"]

        results.append((case, failures))
        mark = "PASS" if not failures else "FAIL"
        print(f"  {mark}  {case['id']:28} {case['why']}")
        if failures:
            for failure in failures:
                print(f"        - {failure}")

    passed = sum(1 for _, failures in results if not failures)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
