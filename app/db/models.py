"""Database tables.

Two tables, and that is deliberate. The full workflow state already lives in
LangGraph's checkpoint tables, which is what makes a trip resumable. These two
exist for the questions the checkpointer cannot answer: "show me this user's
trips", "how many plans went over budget", "what happened to trip X".

Everything that would otherwise be twenty columns is JSONB, because we query
trips by status and id, not by hotel rating.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Trip(Base):
    """One planning session. The queryable view of a trip."""

    __tablename__ = "trips"

    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The LangGraph thread this trip resumes from. Same value as trip_id today,
    # kept separate because they answer different questions.
    thread_id: Mapped[str] = mapped_column(String(64), index=True)

    user_request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)

    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    itinerary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list["TripEvent"]] = relationship(
        back_populates="trip", cascade="all, delete-orphan"
    )


class TripEvent(Base):
    """What happened to a trip, in order. The audit trail behind the logs."""

    __tablename__ = "trip_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(
        ForeignKey("trips.trip_id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trip: Mapped[Trip] = relationship(back_populates="events")
