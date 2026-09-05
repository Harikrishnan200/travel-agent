"""Transport MCP server: flights, trains and buses.

Every tool has an explicit typed signature. FastMCP turns those annotations into
a JSON schema, so the caller cannot pass arbitrary parameters - a bad call is
rejected before any code runs.

Run standalone:
    python -m mcp_servers.transport.server
"""

import logging

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.logging_setup import configure_logging
from mcp_servers.transport import mock_data, provider
from mcp_servers.validation import validate_count, validate_date

logger = logging.getLogger(__name__)
configure_logging()
settings = get_settings()

# Trains and buses have no free API: every MCP server covering Indian rail or
# road sits behind a paid RapidAPI subscription. Those two tools serve mock data
# whatever the configuration, tagged "source": "mock" in the record itself.
#
# Flights are different. AviationStack's free plan gives real airlines, flight
# numbers and timetables - but no fares - so search_flights uses it when real
# data is asked for and a key is present, estimating the price and saying so.

mcp = FastMCP(
    "transport",
    instructions="Search flights, Indian trains and buses between two cities.",
    stateless_http=True,
    json_response=True,
    port=9101,
)


@mcp.tool()
def search_flights(origin: str, destination: str, date: str, passengers: int = 1) -> list[dict]:
    """Search flights between two Indian cities.

    Args:
        origin: Departure city, e.g. "Kochi".
        destination: Arrival city, e.g. "Delhi".
        date: Travel date as YYYY-MM-DD.
        passengers: Number of travellers.

    Returns an empty list when either city has no airport.
    """
    validate_date(date)
    validate_count(passengers, "passengers")

    if not settings.use_mock_data and settings.aviation_stack_api_key:
        try:
            flights = provider.search_flights(
                origin,
                destination,
                date,
                passengers,
                api_key=settings.aviation_stack_api_key,
                timeout=settings.mcp_timeout_seconds * 2,
            )
            if flights:
                return flights
            logger.warning("aviationstack returned no flights, falling back to mock data")
        except provider.FlightProviderError as exc:
            # Real schedules are a bonus, not a dependency. Falling back is safe
            # here because every record says which it is.
            logger.warning("aviationstack unavailable, using mock flights: %s", exc)

    return mock_data.search_flights(origin, destination, date, passengers)


@mcp.tool()
def search_trains(origin: str, destination: str, date: str, passengers: int = 1) -> list[dict]:
    """Search Indian trains between two cities.

    Args:
        origin: Departure city, e.g. "Kochi".
        destination: Arrival city, e.g. "Delhi".
        date: Travel date as YYYY-MM-DD.
        passengers: Number of travellers.

    Returns sleeper (SL) and AC 3-tier (3A) options, cheapest first.
    """
    validate_date(date)
    validate_count(passengers, "passengers")

    # No free provider exists for Indian rail; see the note at the top.
    return mock_data.search_trains(origin, destination, date, passengers)


@mcp.tool()
def search_buses(origin: str, destination: str, date: str, passengers: int = 1) -> list[dict]:
    """Search buses between two Indian cities.

    Args:
        origin: Departure city, e.g. "Kochi".
        destination: Arrival city, e.g. "Bangalore".
        date: Travel date as YYYY-MM-DD.
        passengers: Number of travellers.

    Returns an empty list for routes too long to be realistic by road.
    """
    validate_date(date)
    validate_count(passengers, "passengers")

    # No free provider exists for buses; see the note at the top.
    return mock_data.search_buses(origin, destination, date, passengers)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
