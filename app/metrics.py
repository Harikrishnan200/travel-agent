"""Prometheus metrics.

Four, deliberately. Each one exists because it answers a question someone would
actually ask when the system misbehaves:

- is the primary LLM failing, and are we falling back?
- which tools are slow or erroring, and is the cache doing anything?
- what do plans fail validation for?
- which part of the workflow is slow?

No exporter, no collector, no dashboards. `GET /metrics` and read it.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

llm_calls = Counter(
    "llm_calls_total",
    "LLM calls by provider and outcome.",
    ["provider", "status"],
)

tool_calls = Counter(
    "tool_calls_total",
    "MCP tool calls by tool, outcome and whether the cache answered.",
    ["tool", "status", "cache_hit"],
)

validation_failures = Counter(
    "trip_validation_failures_total",
    "Plan validation failures by the kind of problem found.",
    ["reason"],
)

node_duration = Histogram(
    "node_duration_seconds",
    "How long each workflow node takes.",
    ["node"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


def render() -> tuple[bytes, str]:
    """The metrics page and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST


# Validation errors are free text, so they are grouped into a handful of reasons
# rather than being used as label values directly - one label per wording would
# make the metric useless.
REASON_PATTERNS = (
    ("budget_exceeded", "Budget exceeded"),
    ("no_transport", "No transport"),
    ("no_accommodation", "No accommodation"),
    ("weather_conflict", "outdoor plans"),
    ("bad_dates", "date"),
    ("itinerary_shape", "Itinerary"),
    ("empty_schedule", "nothing planned"),
)


def classify_error(error: str) -> str:
    """Group a validation error into one of a few stable reasons."""
    for reason, fragment in REASON_PATTERNS:
        if fragment.lower() in error.lower():
            return reason
    return "other"
