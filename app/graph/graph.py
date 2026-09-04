"""Builds the LangGraph workflow.

The shape of the graph is the architecture, so this file is kept to just the
nodes, the edges and the three routing decisions.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.graph import nodes
from app.graph.state import TravelState

settings = get_settings()


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
    """Approve, modify or reject. Phase 11 supplies the real human input."""
    if state.get("human_feedback"):
        return "modify"
    if state.get("human_approved"):
        return "approve"
    return "wait"


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


def build_graph(checkpointer=None):
    """Compile the workflow. Pass a checkpointer to persist state (Phase 10)."""
    graph = StateGraph(TravelState)

    graph.add_node("parse_request", nodes.parse_request)
    graph.add_node("validate_request", nodes.validate_request)
    graph.add_node("research_transport", nodes.research_transport)
    graph.add_node("research_accommodation", nodes.research_accommodation)
    graph.add_node("research_experience", nodes.research_experience)
    graph.add_node("create_itinerary", nodes.create_itinerary)
    graph.add_node("validate_plan", nodes.validate_plan)
    graph.add_node("replan", nodes.replan)
    graph.add_node("human_review", nodes.human_review)
    graph.add_node("needs_user_decision", nodes.needs_user_decision)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("output_guard", nodes.output_guard)

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
    graph.add_edge("needs_user_decision", END)

    graph.add_conditional_edges(
        "human_review",
        route_after_human_review,
        {"approve": "finalize", "modify": "replan", "wait": END},
    )

    graph.add_edge("finalize", "output_guard")
    graph.add_edge("output_guard", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
