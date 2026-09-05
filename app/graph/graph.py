"""Builds the LangGraph workflow.

The shape of the graph is the architecture, so this file is kept to just the
nodes, the edges and the three routing decisions.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app import metrics
from app.config import get_settings
from app.graph import nodes
from app.graph.state import TravelState
from app.logging_setup import get_logger

logger = get_logger(__name__)
settings = get_settings()


def instrumented(name: str, node):
    """Time a node and log what it changed.

    Wrapping here means every node is measured without any of them knowing they
    are being measured.
    """

    def run(state: TravelState) -> dict:
        with metrics.node_duration.labels(node=name).time():
            updates = node(state)

        errors = updates.get("validation_errors") if isinstance(updates, dict) else None
        for error in errors or []:
            metrics.validation_failures.labels(reason=metrics.classify_error(error)).inc()

        logger.info(
            "node_finished",
            node=name,
            changed=sorted(updates) if isinstance(updates, dict) else [],
            status=updates.get("status") if isinstance(updates, dict) else None,
        )
        return updates

    return run


# --------------------------------------------------------------------------
# Routing decisions
# --------------------------------------------------------------------------


def route_after_request_validation(state: TravelState) -> str:
    """A request that failed the input guardrail never reaches the agents."""
    return "invalid" if state.get("status") == "invalid_request" else "valid"


def route_after_plan_validation(state: TravelState) -> str:
    """Valid plan -> human. Invalid -> re-plan, until we run out of attempts."""
    if not state.get("validation_errors"):
        return "valid"
    if state.get("replan_attempts", 0) >= settings.max_replan_attempts:
        return "exhausted"
    return "replan"


def route_after_human_review(state: TravelState) -> str:
    """Act on the answer the traveller gave at the interrupt."""
    if state.get("status") == "rejected":
        return "reject"
    if state.get("human_feedback"):
        return "modify"
    if state.get("human_approved"):
        return "approve"
    return "reject"


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


def build_graph(checkpointer=None):
    """Compile the workflow. Pass a checkpointer to persist state (Phase 10)."""
    graph = StateGraph(TravelState)

    # Every node is registered the same way, wrapped so it is timed and logged.
    for name in (
        "parse_request",
        "validate_request",
        "research_transport",
        "research_accommodation",
        "research_experience",
        "create_itinerary",
        "validate_plan",
        "replan",
        "apply_feedback",
        "human_review",
        "needs_user_decision",
        "finalize",
        "output_guard",
    ):
        graph.add_node(name, instrumented(name, getattr(nodes, name)))

    graph.add_edge(START, "parse_request")
    graph.add_edge("parse_request", "validate_request")

    graph.add_conditional_edges(
        "validate_request",
        route_after_request_validation,
        {"valid": "research_transport", "invalid": END},
    )

    # Research runs in sequence: hotel dates depend on the chosen transport.
    graph.add_edge("research_transport", "research_accommodation")
    graph.add_edge("research_accommodation", "research_experience")
    graph.add_edge("research_experience", "create_itinerary")
    graph.add_edge("create_itinerary", "validate_plan")

    graph.add_conditional_edges(
        "validate_plan",
        route_after_plan_validation,
        {
            "valid": "human_review",
            "replan": "replan",
            "exhausted": "needs_user_decision",
        },
    )
    graph.add_edge("replan", "validate_plan")

    # Re-planning gave up, so the traveller is asked to change something. They
    # still get a say: this goes to the same review pause, offering "modify" and
    # "reject" but not "approve".
    graph.add_edge("needs_user_decision", "human_review")

    # A modification changes the requirements, so the experience research runs
    # again (picking up any new interests) before the plan is rebuilt.
    graph.add_edge("apply_feedback", "research_experience")

    graph.add_conditional_edges(
        "human_review",
        route_after_human_review,
        {"approve": "finalize", "modify": "apply_feedback", "reject": END},
    )

    graph.add_edge("finalize", "output_guard")
    graph.add_edge("output_guard", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
