"""Real driving routes, from the public OSRM demo server.

Free and keyless. Distances come from actual road geometry rather than a
straight line with a fudge factor.
"""

import logging

import httpx

from mcp_servers.cities import UnknownCityError, normalize_city
from mcp_servers.places.mock_data import city_coordinates

logger = logging.getLogger(__name__)

API_URL = "https://router.project-osrm.org/route/v1/driving"


def calculate_route(origin: str, destination: str, timeout: float = 10.0) -> dict:
    start, end = normalize_city(origin), normalize_city(destination)
    if start is None:
        raise UnknownCityError(origin)
    if end is None:
        raise UnknownCityError(destination)

    start_lat, start_lon = city_coordinates(start)
    end_lat, end_lon = city_coordinates(end)

    response = httpx.get(
        f"{API_URL}/{start_lon},{start_lat};{end_lon},{end_lat}",
        params={"overview": "false"},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_route(start, end, response.json())


def parse_route(origin: str, destination: str, payload: dict) -> dict:
    """Turn an OSRM response into the same shape the mock provider returns."""
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ValueError(f"OSRM could not route {origin} to {destination}: {payload.get('code')}")

    route = payload["routes"][0]
    return {
        "origin": origin,
        "destination": destination,
        "distance_km": round(route["distance"] / 1000, 1),
        "duration_hours": round(route["duration"] / 3600, 1),
        "mode": "driving",
        "source": "osrm",
    }


# --- Tavily: real descriptions for real places -------------------------------
#
# Tavily searches the web and returns prose. That makes it good for one thing
# here and bad for another:
#
# - good: a sentence about a place, and a link to read more. Grounded in a real
#   page, and if it is wrong the traveller can see the source and judge.
# - bad: numbers. Entry fees and opening hours pulled out of prose are exactly
#   the sort of fact this system computes rather than believes. Its answers are
#   read for description only; every rupee still comes from Python.
#
# The free tier is metered per month, so only the top few places on a search are
# described, and the results are cached for a day.

TAVILY_URL = "https://api.tavily.com/search"

# How many places on one search get a description. Enough to be useful, few
# enough that a month of planning does not exhaust the free tier.
MAX_DESCRIBED = 3
DESCRIPTION_TTL_SECONDS = 86400


def describe_place(name: str, city: str, api_key: str, timeout: float = 15.0) -> dict | None:
    """A sentence about a place and where it came from, or None."""
    from app import cache

    key = cache.make_key("tavily_describe", {"name": name, "city": city})
    cached = cache.get(key)
    if cached is not None:
        return cached or None

    try:
        response = httpx.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": f"{name} {city} India visitor information",
                "max_results": 2,
                "include_answer": True,
                "search_depth": "basic",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        described = parse_description(response.json())
    except Exception as exc:
        logger.warning("tavily lookup failed for %s: %s", name, exc)
        return None

    # Cache the miss too, so a place with nothing written about it is not looked
    # up again on every plan.
    cache.set(key, described or {}, DESCRIPTION_TTL_SECONDS)
    return described


def parse_description(payload: dict) -> dict | None:
    """Pull a description and a source link out of a Tavily response."""
    answer = (payload.get("answer") or "").strip()
    results = payload.get("results") or []
    if not answer and not results:
        return None

    about = answer or (results[0].get("content") or "").strip()
    if not about:
        return None

    return {
        "about": about[:400],
        "learn_more": results[0].get("url") if results else None,
        "described_by": "tavily",
    }


def describe_places(
    places: list[dict], city: str, api_key: str, timeout: float = 15.0
) -> list[dict]:
    """Add descriptions to the first few places, leaving the rest untouched."""
    enriched = []
    for index, place in enumerate(places):
        if index < MAX_DESCRIBED and (
            described := describe_place(place["name"], city, api_key, timeout)
        ):
            enriched.append({**place, **described})
        else:
            enriched.append(place)
    return enriched
