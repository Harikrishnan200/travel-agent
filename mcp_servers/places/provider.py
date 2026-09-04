"""Real driving routes, from the public OSRM demo server.

Free and keyless. Distances come from actual road geometry rather than a
straight line with a fudge factor.
"""

import httpx

from mcp_servers.cities import UnknownCityError, normalize_city
from mcp_servers.places.mock_data import city_coordinates

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
