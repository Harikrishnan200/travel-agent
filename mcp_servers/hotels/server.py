"""Hotel MCP server.

Run standalone:
    python -m mcp_servers.hotels.server
"""

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from mcp_servers.hotels import mock_data, provider
from mcp_servers.validation import validate_count, validate_date

settings = get_settings()

mcp = FastMCP(
    "hotels",
    instructions="Search hotels in an Indian city for a date range.",
    stateless_http=True,
    json_response=True,
    port=9102,
)


@mcp.tool()
async def search_hotels(
    city: str,
    check_in: str,
    check_out: str,
    guests: int = 1,
    budget: float | None = None,
) -> list[dict]:
    """Search hotels in a city.

    Args:
        city: City to stay in, e.g. "Delhi".
        check_in: Arrival date as YYYY-MM-DD.
        check_out: Departure date as YYYY-MM-DD, after check_in.
        guests: Number of guests; rooms are allocated two guests per room.
        budget: Optional cap on the total price for the whole stay.

    Returns hotels sorted by rating, each with a total price for the stay.
    """
    validate_date(check_in, "check_in")
    validate_date(check_out, "check_out")
    validate_count(guests, "guests")
    if budget is not None and budget <= 0:
        raise ValueError(f"budget must be positive, got {budget}")

    if settings.use_mock_data:
        return mock_data.search_hotels(city, check_in, check_out, guests, budget)
    # trivago's public MCP server: live prices, no API key. See provider.py for
    # why its output is stripped before we look at it.
    return await provider.search_hotels(
        city, check_in, check_out, guests, budget, timeout=settings.hotel_search_timeout_seconds
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
