"""Postgres persistence and resuming a trip.

These run against a real Postgres and skip themselves when there is not one, so
the rest of the suite stays independent of a running database. Each test works
in its own schema-less namespace by using unique trip ids.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.api import trips
from app.db import database
from app.db.models import Base
from app.graph.graph import build_graph
from app.graph.state import new_state
from app.main import app


@pytest.fixture
def postgres():
    """A real database, or skip. Restores test isolation afterwards."""
    from sqlalchemy import create_engine

    from app.config import get_settings

    try:
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        with engine.connect():
            pass
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"postgres is not available: {exc}")

    Base.metadata.create_all(engine)
    database.set_engine(engine)
    yield engine
    database.set_engine(None)


@pytest.fixture
def trip_id():
    return f"test-{uuid.uuid4()}"


# --- the trip record --------------------------------------------------------


def test_a_trip_is_saved_and_read_back(postgres, trip_id):
    database.save_trip(
        trip_id,
        {
            "user_request": "Kochi to Delhi",
            "status": "awaiting_approval",
            "origin": "Kochi",
            "destination": "Delhi",
            "total_cost": 43500.0,
            "itinerary": {"days": [{"day": 1}]},
        },
        plan={"origin": "Kochi"},
    )

    stored = database.get_trip(trip_id)
    assert stored.status == "awaiting_approval"
    assert stored.destination == "Delhi"
    assert float(stored.total_cost) == 43500.0
    assert stored.itinerary == {"days": [{"day": 1}]}


def test_saving_the_same_trip_twice_updates_it(postgres, trip_id):
    base = {"user_request": "x", "status": "planning"}
    database.save_trip(trip_id, base)
    database.save_trip(trip_id, {**base, "status": "approved", "total_cost": 100.0})

    stored = database.get_trip(trip_id)
    assert stored.status == "approved"
    assert float(stored.total_cost) == 100.0


def test_events_are_appended_in_order(postgres, trip_id):
    database.save_trip(trip_id, {"user_request": "x", "status": "planning"})
    database.record_event(trip_id, "trip_requested", {"message": "x"})
    database.record_event(trip_id, "planning_finished", {"status": "awaiting_approval"})

    events = database.get_events(trip_id)
    assert [event.event_type for event in events] == ["trip_requested", "planning_finished"]
    assert events[1].payload["status"] == "awaiting_approval"


def test_recent_trips_are_listed(postgres, trip_id):
    database.save_trip(trip_id, {"user_request": "x", "status": "planning"})
    assert any(trip.trip_id == trip_id for trip in database.list_trips(limit=100))


# --- resuming ---------------------------------------------------------------


def test_a_trip_resumes_from_a_new_graph_instance(postgres, trip_id):
    """The point of the checkpointer: a fresh process picks the trip back up."""
    from app.db.checkpointer import get_checkpointer, set_checkpointer

    set_checkpointer(None)
    checkpointer = get_checkpointer()
    if isinstance(checkpointer, MemorySaver):  # pragma: no cover
        pytest.skip("postgres checkpointer unavailable")

    config = {"configurable": {"thread_id": trip_id}}
    first = build_graph(checkpointer).invoke(
        new_state(trip_id, "Plan a 5 day trip from Kochi to Delhi for 2"), config=config
    )

    # A different graph object, as a restarted process would have.
    resumed = build_graph(checkpointer).get_state(config)

    assert resumed.values["status"] == first["status"]
    assert resumed.values["destination"] == "Delhi"
    assert resumed.values["total_cost"] == first["total_cost"]
    set_checkpointer(None)


# --- through the API --------------------------------------------------------


def test_planning_through_the_api_writes_a_trip_and_its_events(postgres):
    trips.reset_graph()
    client = TestClient(app)

    created = client.post(
        "/api/v1/trips", json={"message": "Plan a 5 day trip from Kochi to Delhi for 2 people"}
    ).json()

    stored = database.get_trip(created["trip_id"])
    assert stored is not None
    assert stored.status == created["status"]

    events = client.get(f"/api/v1/trips/{created['trip_id']}/events").json()
    assert [event["event_type"] for event in events] == [
        "trip_requested",
        "planning_finished",
    ]
    trips.reset_graph()


def test_a_trip_survives_a_lost_checkpoint(postgres):
    """If the checkpoint is gone the row still answers, rather than a 404."""
    trips.reset_graph()
    client = TestClient(app)
    trip_id = client.post(
        "/api/v1/trips", json={"message": "Plan a 4 day trip from Delhi to Jaipur for 2"}
    ).json()["trip_id"]

    # Simulate a restart that lost the checkpoints but kept the database.
    from app.db import checkpointer as checkpointer_module

    checkpointer_module.set_checkpointer(MemorySaver())
    trips.reset_graph()

    response = client.get(f"/api/v1/trips/{trip_id}")
    assert response.status_code == 200
    assert response.json()["plan"]["destination"] == "Jaipur"
    trips.reset_graph()


# --- degrading without a database -------------------------------------------


def test_planning_works_with_no_database():
    """Postgres is where a trip is remembered, not where it is planned."""
    database.set_engine(None)
    trips.reset_graph()
    client = TestClient(app)

    response = client.post(
        "/api/v1/trips", json={"message": "Plan a 3 day trip from Delhi to Agra for 2"}
    )
    assert response.status_code == 201
    assert response.json()["status"] in {"awaiting_approval", "needs_user_decision"}


def test_reads_return_nothing_rather_than_failing_with_no_database():
    database.set_engine(None)
    assert database.get_trip("anything") is None
    assert database.list_trips() == []
    assert database.get_events("anything") == []
