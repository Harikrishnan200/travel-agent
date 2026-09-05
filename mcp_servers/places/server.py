"""Places MCP server: attractions and point-to-point routes.

Routes live here rather than in a server of their own: both tools answer
questions about geography and read the same city table.

Run standalone:
    python -m mcp_servers.places.server
"""

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from mcp_servers.places import mock_data, provider
from mcp_servers.validation import validate_count

settings = get_settings()

# Attractions have no free, keyless API either, so search_places always serves
# mock data and tags every record with "source": "mock". calculate_route does
# have one (OSRM), so it honours USE_MOCK_DATA.
PLACES_ARE_MOCK_ONLY = True

mcp = FastMCP(
    "places",
    instructions="Search attractions in an Indian city and measure routes between cities.",
    stateless_http=True,
    json_response=True,
    port=9103,
)


@mcp.tool()
def search_places(city: str, category: str | None = None, max_results: int = 10) -> list[dict]:
    """Search attractions and activities in a city.

    Args:
        city: City to search, e.g. "Delhi".
        category: One of historical, nature, food, culture, adventure, shopping.
            Omit for all categories.
        max_results: Maximum number of places to return.

    Each place reports an entry fee, a typical visit duration and whether it is
    outdoors, so the caller can schedule around the weather.
    """
    validate_count(max_results, "max_results", maximum=50)

    # Always mock: see PLACES_ARE_MOCK_ONLY above.
    return mock_data.search_places(city, category, max_results)


@mcp.tool()
def calculate_route(origin: str, destination: str) -> dict:
    """Road distance and driving time between two cities.

    Args:
        origin: Starting city, e.g. "Delhi".
        destination: Ending city, e.g. "Agra".

    This is a distance estimate, not turn-by-turn navigation.
    """
    if settings.use_mock_data:
        return mock_data.calculate_route(origin, destination)
    return provider.calculate_route(origin, destination, timeout=settings.mcp_timeout_seconds)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
