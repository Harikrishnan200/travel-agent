"""Structured logging and metrics.

The point of these is that a trip can be followed end to end in the logs, and
that nothing secret ever gets there.
"""

import pytest
import structlog
from fastapi.testclient import TestClient

from app import metrics
from app.logging_setup import drop_empty, log_context, new_request_id, redact_secrets
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def captured():
    """Run a log line through the exact processor chain configure_logging installs.

    Testing the processors directly, in order, is both simpler and closer to
    what actually happens than driving a logger and reading it back.
    """

    def emit(event, **fields):
        entry = {**fields, "event": event}
        entry = drop_empty(None, "info", entry)
        entry = structlog.contextvars.merge_contextvars(None, "info", entry)
        return redact_secrets(None, "info", entry)

    return emit


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["api_key", "API_KEY", "groq_api_key", "password", "secret", "token", "authorization"]
)
def test_secret_fields_never_reach_the_logs(captured, field):
    assert captured("call", **{field: "hunter2"})[field] == "[redacted]"


def test_a_credential_shaped_value_is_redacted_wherever_it_appears(captured):
    """Even in an innocently named field, because that is how leaks actually happen."""
    line = captured("call", note="using gsk_abcdefghijklmnopqrstuvwx today")
    assert "gsk_" not in line["note"]
    assert "[redacted]" in line["note"]


@pytest.mark.parametrize(
    "value",
    ["sk-abcdefghijklmnopqrstuvwx", "gsk_abcdefghijklmnopqrstuv", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWX"],
)
def test_every_known_key_shape_is_caught(captured, value):
    assert value not in captured("call", detail=value)["detail"]


def test_ordinary_fields_are_left_alone(captured):
    line = captured("tool_call", tool="search_trains", duration_ms=0.8)
    assert line["tool"] == "search_trains"
    assert line["duration_ms"] == 0.8


# --- correlation ------------------------------------------------------------


def test_bound_context_appears_on_every_line(captured):
    with log_context(request_id="req-1", trip_id="trip-9"):
        line = captured("tool_call", tool="search_trains")
    assert line["request_id"] == "req-1"
    assert line["trip_id"] == "trip-9"


def test_a_none_does_not_shadow_the_bound_trip_id(captured):
    """A caller that does not know the trip id must not erase it from the line."""
    with log_context(trip_id="trip-9"):
        line = captured("tool_call", tool="search_trains", trip_id=None)
    assert line["trip_id"] == "trip-9"


def test_an_explicit_value_still_wins(captured):
    with log_context(trip_id="trip-9"):
        line = captured("tool_call", trip_id="explicit")
    assert line["trip_id"] == "explicit"


def test_empty_fields_are_dropped(captured):
    assert "error" not in captured("tool_call", tool="x", error=None)


def test_context_is_released_afterwards(captured):
    with log_context(trip_id="trip-9"):
        pass
    assert "trip_id" not in captured("later")


def test_request_ids_are_unique():
    assert new_request_id() != new_request_id()


# --- through the API --------------------------------------------------------


def test_every_response_carries_a_request_id(client):
    assert client.get("/health").headers["X-Request-ID"]


def test_a_supplied_request_id_is_kept(client):
    """So a caller can trace a request they already have an id for."""
    response = client.get("/health", headers={"X-Request-ID": "my-trace-42"})
    assert response.headers["X-Request-ID"] == "my-trace-42"


# --- metrics ----------------------------------------------------------------


def test_metrics_are_exposed_for_prometheus(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_planning_a_trip_records_tool_calls_and_node_timings(client):
    client.post("/api/v1/trips", json={"message": "Plan a 5 day trip from Kochi to Delhi for 2"})
    body = client.get("/metrics").text

    assert 'tool_calls_total{cache_hit="false",status="ok",tool="search_trains"}' in body
    assert 'node_duration_seconds_count{node="validate_plan"}' in body


def test_a_cache_hit_is_visible_in_the_metrics(client):
    """Otherwise there is no way to tell whether the cache is doing anything."""
    message = {"message": "Plan a 5 day trip from Kochi to Delhi for 2 people"}
    client.post("/api/v1/trips", json=message)
    client.post("/api/v1/trips", json=message)

    assert 'tool_calls_total{cache_hit="true"' in client.get("/metrics").text


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("Budget exceeded by Rs 7,000.", "budget_exceeded"),
        ("No transport was found from Kochi to Delhi.", "no_transport"),
        ("No accommodation was found in Delhi.", "no_accommodation"),
        ("Day 2 has outdoor plans (Red Fort) in rain.", "weather_conflict"),
        ("End date is before the start date.", "bad_dates"),
        ("Something else entirely.", "other"),
    ],
)
def test_validation_errors_are_grouped_into_stable_reasons(error, reason):
    """Free-text errors as label values would make the metric useless."""
    assert metrics.classify_error(error) == reason


# --- redaction in library logs ----------------------------------------------
# structlog covers our own lines. Everything else is written by libraries, which
# is where a key is most likely to escape: httpx logs the URLs it fetches, and a
# key in a query string goes straight into the logs with it.


def test_a_key_in_a_query_string_is_removed():
    """Found for real: httpx logged an AviationStack URL with the key in it."""
    from app.logging_setup import redact_text

    leaked = "HTTP Request: GET https://api.aviationstack.com/v1/flights?access_key=abc123def456"
    assert "abc123def456" not in redact_text(leaked)
    assert "access_key=[redacted]" in redact_text(leaked)


@pytest.mark.parametrize("param", ["access_key", "api_key", "apikey", "token", "password"])
def test_every_credential_parameter_name_is_covered(param):
    from app.logging_setup import redact_text

    assert "s3cret" not in redact_text(f"GET https://example.com/x?{param}=s3cret&other=1")


def test_the_rest_of_the_url_survives():
    from app.logging_setup import redact_text

    cleaned = redact_text("GET https://api.x.com/v1/flights?access_key=s3cret&dep_iata=COK")
    assert "dep_iata=COK" in cleaned


def test_library_records_keep_their_number_formatting(caplog):
    """The filter must not stringify args: uvicorn formats records with %d."""
    import logging

    from app.logging_setup import RedactingFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s - %d", ("GET /health", 200), None
    )
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "GET /health - 200"


def test_a_secret_in_a_string_argument_is_still_removed():
    import logging

    from app.logging_setup import RedactingFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "fetched %s in %dms",
        ("https://x.com?api_key=s3cretvalue", 12),
        None,
    )
    assert RedactingFilter().filter(record)
    assert "s3cretvalue" not in record.getMessage()
    assert "12ms" in record.getMessage()


def test_a_secret_in_a_non_string_argument_is_still_removed():
    """The one that actually leaked: httpx logs the URL as an httpx.URL, not a str."""
    import logging

    import httpx

    from app.logging_setup import RedactingFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", httpx.URL("https://api.x.com/v1?access_key=s3cretvalue"), "HTTP/1.1", 200, "OK"),
        None,
    )
    assert RedactingFilter().filter(record)

    message = record.getMessage()
    assert "s3cretvalue" not in message
    assert "access_key=[redacted]" in message
    assert "200" in message  # the number survived


def test_objects_without_secrets_are_not_stringified():
    """Only arguments that actually carry a secret are replaced."""
    import logging
    from pathlib import Path

    from app.logging_setup import RedactingFilter

    original = Path("/tmp/harmless.txt")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s took %d", (original, 5), None)
    assert RedactingFilter().filter(record)
    assert record.args[0] is original  # untouched, still a Path
    assert record.args[1] == 5
