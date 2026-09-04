"""Weather MCP server.

Run standalone:
    python -m mcp_servers.weather.server
"""

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from mcp_servers.validation import validate_date
from mcp_servers.weather import mock_data

settings = get_settings()

mcp = FastMCP(
    "weather",
    instructions="Daily weather forecast for an Indian city.",
    stateless_http=True,
    json_response=True,
    port=9104,
)


@mcp.tool()
def get_weather(city: str, date: str) -> dict:
    """Weather forecast for a city on a date.

    Args:
        city: City to forecast, e.g. "Delhi".
        date: Date as YYYY-MM-DD.

    Returns temperature, condition, rain probability and a good_for_outdoor flag
    the caller can schedule against.
    """
    validate_date(date)

    if settings.use_mock_data:
        return mock_data.get_weather(city, date)
    # Phase 6 wires this to Open-Meteo, which is free and needs no key.
    raise NotImplementedError("Real weather provider arrives in Phase 6.")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
