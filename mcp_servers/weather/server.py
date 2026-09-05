"""Weather MCP server.

Run standalone:
    python -m mcp_servers.weather.server
"""

import logging

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.logging_setup import configure_logging
from mcp_servers.validation import validate_date
from mcp_servers.weather import mock_data, provider

configure_logging()
logger = logging.getLogger(__name__)
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
    the caller can schedule against. Beyond the forecast window the figures are
    seasonal averages, and "source" says so.
    """
    validate_date(date)

    if settings.use_mock_data:
        return mock_data.get_weather(city, date)

    try:
        return provider.get_weather(city, date, timeout=settings.mcp_timeout_seconds)
    except provider.OutsideForecastWindow:
        # No forecast exists that far ahead, and a trip planned two months out is
        # a normal request. Seasonal averages are more useful than nothing, so
        # long as the record says that is what they are.
        logger.info("no forecast for %s, using seasonal averages", date)
        return {**mock_data.get_weather(city, date), "source": "seasonal-average"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
