"""Database connection and the handful of queries the API needs.

Postgres is where a trip survives a restart. If it is unreachable the system
still plans - it just cannot remember - so every write here is best-effort and
logs rather than raising.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, Trip, TripEvent

logger = logging.getLogger(__name__)
settings = get_settings()

_engine = None
_session_factory = None
_unavailable = False


def get_engine():
    """The shared engine, or None if Postgres cannot be reached."""
    global _engine, _session_factory, _unavailable
    if _unavailable:
        return None
    if _engine is None:
        try:
            _engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
            with _engine.connect():
                pass
            _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
        except Exception as exc:
            logger.warning("postgres unavailable, trips will not be persisted: %s", exc)
            _engine, _session_factory, _unavailable = None, None, True
    return _engine


def create_tables() -> bool:
    """Create the tables if they are not there. Returns whether the database is usable."""
    engine = get_engine()
    if engine is None:
        return False
    Base.metadata.create_all(engine)
    return True


def set_engine(engine) -> None:
    """Point the module at a specific engine. Used by tests."""
    global _engine, _session_factory, _unavailable
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False) if engine else None
    _unavailable = engine is None


@contextmanager
def session_scope() -> Iterator[Session | None]:
    """A transaction, or None when there is no database. Never raises upward."""
    if get_engine() is None:
        yield None
        return

    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("database write failed, continuing without persistence: %s", exc)
    finally:
        session.close()


# --- the queries the API actually makes -------------------------------------


def save_trip(trip_id: str, state: dict, plan: dict | None = None) -> None:
    """Insert or update the queryable record of a trip."""
    with session_scope() as session:
        if session is None:
            return
        trip = session.get(Trip, trip_id)
        if trip is None:
            trip = Trip(
                trip_id=trip_id, thread_id=trip_id, user_request=state.get("user_request", "")
            )
            session.add(trip)

        trip.status = state.get("status", "unknown")
        trip.origin = state.get("origin")
        trip.destination = state.get("destination")
        trip.total_cost = state.get("total_cost")
        trip.itinerary = state.get("itinerary")
        trip.plan = plan


def record_event(trip_id: str, event_type: str, payload: dict | None = None) -> None:
    """Append one line to a trip's audit trail."""
    with session_scope() as session:
        if session is None:
            return
        session.add(TripEvent(trip_id=trip_id, event_type=event_type, payload=payload))


def get_trip(trip_id: str) -> Trip | None:
    with session_scope() as session:
        if session is None:
            return None
        return session.get(Trip, trip_id)


def list_trips(limit: int = 20) -> list[Trip]:
    with session_scope() as session:
        if session is None:
            return []
        statement = select(Trip).order_by(Trip.created_at.desc()).limit(limit)
        return list(session.scalars(statement))


def get_events(trip_id: str) -> list[TripEvent]:
    with session_scope() as session:
        if session is None:
            return []
        statement = select(TripEvent).where(TripEvent.trip_id == trip_id).order_by(TripEvent.id)
        return list(session.scalars(statement))
