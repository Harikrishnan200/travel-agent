"""Experience Agent: places, weather and distances for the destination.

Tools it may call: search_places, get_weather, calculate_route. Nothing else.

It also proposes which day each activity should fall on, because that decision
needs the weather it just fetched. The itinerary node turns the proposal into
the final schedule.
"""

import logging
from datetime import date, timedelta

from app.agents.schemas import DayWeather, ExperienceResult, Place, Route

logger = logging.getLogger(__name__)

# Activities per day. Enough to fill morning/afternoon/evening without a rush.
ACTIVITIES_PER_DAY = 3


def research_experience(brief: dict, tools: dict) -> ExperienceResult:
    """Fetch places and weather, then suggest a weather-aware day-by-day split."""
    failures: list[str] = []
    notes: list[str] = []

    places = _fetch_places(brief, tools, failures)
    weather = _fetch_weather(brief, tools, failures)
    routes = _fetch_route(brief, tools, failures)

    schedule = _suggest_schedule(places, weather, brief, notes)

    return ExperienceResult(
        places=places,
        weather=weather,
        routes=routes,
        suggested_schedule=schedule,
        notes=notes,
        failures=failures,
    )


def _fetch_places(brief: dict, tools: dict, failures: list[str]) -> list[Place]:
    """One search per interest, so the traveller's stated interests drive results."""
    interests = brief.get("interests") or [None]
    found: dict[str, Place] = {}
    for interest in interests:
        try:
            raw = tools["search_places"](
                city=brief["city"], category=interest, max_results=brief.get("max_places", 8)
            )
            for item in raw:
                found[item["name"]] = Place(**item)
        except Exception as exc:
            logger.warning("places search failed: category=%s error=%s", interest, exc)
            failures.append(f"places search unavailable for {interest or 'all categories'}")
    return sorted(found.values(), key=lambda p: -p.rating)


def _fetch_weather(brief: dict, tools: dict, failures: list[str]) -> list[DayWeather]:
    """A forecast per trip day. A missing day is skipped, not faked."""
    forecasts = []
    for day in _trip_dates(brief):
        try:
            forecasts.append(DayWeather(**tools["get_weather"](city=brief["city"], date=day)))
        except Exception as exc:
            logger.warning("weather lookup failed: date=%s error=%s", day, exc)
            failures.append(f"weather unavailable for {day}")
    return forecasts


def _fetch_route(brief: dict, tools: dict, failures: list[str]) -> list[Route]:
    """Distance from the arrival point to the destination city centre."""
    if brief.get("origin") == brief.get("city"):
        return []
    try:
        raw = tools["calculate_route"](origin=brief["origin"], destination=brief["city"])
        return [Route(**raw)]
    except Exception as exc:
        logger.warning("route calculation failed: error=%s", exc)
        failures.append("route calculation unavailable")
        return []


def _trip_dates(brief: dict) -> list[str]:
    start = date.fromisoformat(brief["start_date"])
    return [(start + timedelta(days=i)).isoformat() for i in range(brief["duration_days"])]


def _suggest_schedule(
    places: list[Place], weather: list[DayWeather], brief: dict, notes: list[str]
) -> list[dict]:
    """Put outdoor activities on the driest days and indoor ones on wet days.

    This is the weather-aware ordering the brief asks for, done with a sort
    rather than an optimiser. Places are dealt out round-robin so a short list
    spreads across every day instead of filling the first few.
    """
    days = _trip_dates(brief)
    if not places:
        return [{"date": day, "activities": []} for day in days]

    forecast_by_date = {w.date: w for w in weather}
    outdoor = [p for p in places if p.outdoor]
    indoor = [p for p in places if not p.outdoor]

    # Driest days first, so the best outdoor activities land on the best days.
    ranked_days = sorted(
        days, key=lambda d: forecast_by_date[d].rain_probability if d in forecast_by_date else 50
    )
    capacity = {day: _day_capacity(day, days) for day in days}
    assignment: dict[str, list[Place]] = {day: [] for day in days}

    # Deal one activity to each day per round, so nothing is left empty.
    for _ in range(ACTIVITIES_PER_DAY):
        for day in ranked_days:
            if len(assignment[day]) >= capacity[day] or not (outdoor or indoor):
                continue
            forecast = forecast_by_date.get(day)
            wet = forecast is not None and not forecast.good_for_outdoor
            first, second = (indoor, outdoor) if wet else (outdoor, indoor)
            assignment[day].append((first or second).pop(0))

    for day in days:
        forecast = forecast_by_date.get(day)
        scheduled = assignment[day]
        if forecast and not forecast.good_for_outdoor and scheduled:
            outdoor_anyway = [p.name for p in scheduled if p.outdoor]
            note = f"{day}: {forecast.condition} ({forecast.rain_probability}% rain)"
            note += (
                f" - {', '.join(outdoor_anyway)} is outdoors; consider an indoor alternative."
                if outdoor_anyway
                else " - indoor activities scheduled."
            )
            notes.append(note)

    return [
        {
            "date": day,
            "activities": [p.model_dump() for p in assignment[day]],
            "weather": forecast_by_date[day].model_dump() if day in forecast_by_date else None,
        }
        for day in days
    ]


def _day_capacity(day: str, days: list[str]) -> int:
    """Arrival and departure days are partly spent travelling, so they hold less."""
    if len(days) > 1 and day in (days[0], days[-1]):
        return 1
    return ACTIVITIES_PER_DAY
