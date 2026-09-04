"""Every MCP tool, exercised through the MCP layer itself.

These call FastMCP's own `call_tool`, so the tool schema, argument coercion and
the validation inside each tool are all under test - not just the provider
functions underneath.
"""

import json

import pytest

from gateway.policy import READ_TOOLS
from mcp_servers.hotels.server import mcp as hotels_mcp
from mcp_servers.places.server import mcp as places_mcp
from mcp_servers.transport.server import mcp as transport_mcp
from mcp_servers.weather.server import mcp as weather_mcp

SERVERS = {
    "transport": transport_mcp,
    "hotels": hotels_mcp,
    "places": places_mcp,
    "weather": weather_mcp,
}


async def call(server, tool: str, **arguments):
    """Call a tool and return its result as plain Python.

    FastMCP returns (content_blocks, structured_result) when it can build a
    structured result, and content blocks alone otherwise. This helper hides
    that difference so the tests read cleanly.
    """
    outcome = await server.call_tool(tool, arguments)
    if isinstance(outcome, tuple):
        structured = outcome[1]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    return json.loads(outcome[0].text)


# --- registration and schemas ----------------------------------------------


async def test_the_four_servers_expose_exactly_the_read_tools():
    exposed = set()
    for server in SERVERS.values():
        exposed |= {tool.name for tool in await server.list_tools()}
    assert exposed == READ_TOOLS


async def test_every_tool_has_a_description_and_a_typed_schema():
    for name, server in SERVERS.items():
        for tool in await server.list_tools():
            assert tool.description, f"{name}.{tool.name} has no description"
            properties = tool.inputSchema["properties"]
            assert properties, f"{name}.{tool.name} accepts no typed parameters"
            for field, schema in properties.items():
                assert "type" in schema or "anyOf" in schema, f"{tool.name}.{field} is untyped"


async def test_required_parameters_are_marked_required():
    tools = {tool.name: tool for tool in await transport_mcp.list_tools()}
    required = tools["search_flights"].inputSchema["required"]
    assert set(required) == {"origin", "destination", "date"}


# --- transport tools --------------------------------------------------------


async def test_search_flights_returns_priced_options():
    flights = await call(
        transport_mcp,
        "search_flights",
        origin="Kochi",
        destination="Delhi",
        date="2026-09-18",
        passengers=2,
    )
    assert flights
    assert all(f["mode"] == "flight" and f["total_price"] > 0 for f in flights)


async def test_search_trains_returns_sleeper_and_ac_classes():
    trains = await call(
        transport_mcp,
        "search_trains",
        origin="Kochi",
        destination="Delhi",
        date="2026-09-18",
        passengers=1,
    )
    assert {t["travel_class"] for t in trains} == {"SL", "3A"}


async def test_search_buses_is_empty_on_a_very_long_route():
    buses = await call(
        transport_mcp,
        "search_buses",
        origin="Kochi",
        destination="Delhi",
        date="2026-09-18",
        passengers=1,
    )
    assert buses == []


# --- hotel tool -------------------------------------------------------------


async def test_search_hotels_prices_the_whole_stay():
    hotels = await call(
        hotels_mcp,
        "search_hotels",
        city="Delhi",
        check_in="2026-09-18",
        check_out="2026-09-22",
        guests=2,
    )
    assert hotels
    assert all(h["nights"] == 4 and h["total_price"] > h["price_per_night"] for h in hotels)


async def test_search_hotels_honours_a_budget():
    hotels = await call(
        hotels_mcp,
        "search_hotels",
        city="Delhi",
        check_in="2026-09-18",
        check_out="2026-09-22",
        guests=2,
        budget=9000,
    )
    assert all(h["total_price"] <= 9000 for h in hotels)


# --- places tools -----------------------------------------------------------


async def test_search_places_filters_by_category():
    places = await call(places_mcp, "search_places", city="Delhi", category="food")
    assert places
    assert all(p["category"] == "food" for p in places)


async def test_calculate_route_returns_distance_and_time():
    route = await call(places_mcp, "calculate_route", origin="Delhi", destination="Agra")
    assert route["distance_km"] > 0
    assert route["duration_hours"] > 0


# --- weather tool -----------------------------------------------------------


async def test_get_weather_returns_a_schedulable_forecast():
    forecast = await call(weather_mcp, "get_weather", city="Delhi", date="2026-09-18")
    assert 0 <= forecast["rain_probability"] <= 100
    assert isinstance(forecast["good_for_outdoor"], bool)


# --- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("server", "tool", "arguments", "expected"),
    [
        (
            transport_mcp,
            "search_flights",
            {"origin": "Kochi", "destination": "Delhi", "date": "18-09-2026"},
            "YYYY-MM-DD",
        ),
        (
            transport_mcp,
            "search_flights",
            {"origin": "Kochi", "destination": "Delhi", "date": "2026-09-18", "passengers": 0},
            "between 1 and 20",
        ),
        (
            transport_mcp,
            "search_flights",
            {"origin": "Atlantis", "destination": "Delhi", "date": "2026-09-18"},
            "Unknown city",
        ),
        (
            hotels_mcp,
            "search_hotels",
            {"city": "Delhi", "check_in": "2026-09-22", "check_out": "2026-09-18"},
            "must be after",
        ),
        (
            hotels_mcp,
            "search_hotels",
            {"city": "Delhi", "check_in": "2026-09-18", "check_out": "2026-09-22", "budget": -5},
            "must be positive",
        ),
        (
            places_mcp,
            "search_places",
            {"city": "Delhi", "category": "teleportation"},
            "Unknown category",
        ),
        (weather_mcp, "get_weather", {"city": "Delhi", "date": "not-a-date"}, "YYYY-MM-DD"),
    ],
)
async def test_bad_arguments_are_rejected(server, tool, arguments, expected):
    with pytest.raises(Exception, match=expected):
        await server.call_tool(tool, arguments)


async def test_missing_required_arguments_are_rejected():
    with pytest.raises(Exception, match="validation error"):
        await transport_mcp.call_tool("search_flights", {"origin": "Kochi"})


async def test_unknown_tools_are_rejected():
    with pytest.raises(Exception, match="[Uu]nknown tool"):
        await transport_mcp.call_tool("drop_database", {})


async def test_results_are_json_serialisable():
    """The gateway forwards these over HTTP, so they must survive a round trip."""
    flights = await call(
        transport_mcp,
        "search_flights",
        origin="Kochi",
        destination="Delhi",
        date="2026-09-18",
    )
    assert json.loads(json.dumps(flights)) == flights
