"""Contracts between the Travel Manager and the specialised agents.

Agents return these models, never free text, so the manager never has to parse
prose. Every model carries `failures` so a degraded result (one MCP server down)
is reported honestly instead of silently looking like a complete one.
"""

from pydantic import BaseModel, Field


class TransportOption(BaseModel):
    id: str
    mode: str  # flight | train | bus
    provider: str
    origin: str
    destination: str
    date: str
    depart_time: str = ""
    duration_hours: float
    price_per_person: float
    total_price: float


class TransportResult(BaseModel):
    recommended: TransportOption | None = None
    alternatives: list[TransportOption] = Field(default_factory=list)
    reason: str = ""
    failures: list[str] = Field(default_factory=list)


class Hotel(BaseModel):
    name: str
    city: str
    location: str
    rating: float
    price_per_night: float
    rooms_required: int
    nights: int
    total_price: float
    amenities: list[str] = Field(default_factory=list)


class AccommodationResult(BaseModel):
    recommended: Hotel | None = None
    alternatives: list[Hotel] = Field(default_factory=list)
    reason: str = ""
    failures: list[str] = Field(default_factory=list)


class Place(BaseModel):
    name: str
    city: str
    category: str
    rating: float
    entry_fee: float
    visit_duration_hours: float
    outdoor: bool


class DayWeather(BaseModel):
    city: str
    date: str
    temperature_c: float
    condition: str
    rain_probability: int
    good_for_outdoor: bool


class Route(BaseModel):
    origin: str
    destination: str
    distance_km: float
    duration_hours: float


class ExperienceResult(BaseModel):
    places: list[Place] = Field(default_factory=list)
    weather: list[DayWeather] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    suggested_schedule: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
