"""Transport MCP server: flights, trains and buses.

Every tool has an explicit typed signature. FastMCP turns those annotations into
a JSON schema, so the caller cannot pass arbitrary parameters - a bad call is
rejected before any code runs.

Run standalone:
    python -m mcp_servers.transport.server
"""

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from mcp_servers.transport import mock_data
from mcp_servers.validation import validate_count, validate_date

settings = get_settings()

# No free, keyless API exists for Indian flights, trains or buses - the MCP
# servers that cover them all sit behind a paid RapidAPI subscription. These
# tools therefore always serve mock data, and every record they return carries
# "source": "mock" so a consumer can see that in the data and not just here.
MOCK_ONLY = True

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

    # Always mock: see MOCK_ONLY above.
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

    # Always mock: see MOCK_ONLY above.
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

    # Always mock: see MOCK_ONLY above.
    return mock_data.search_buses(origin, destination, date, passengers)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
