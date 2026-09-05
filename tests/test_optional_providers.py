"""The two optional providers: AviationStack flights and Tavily descriptions.

Both are used only when their key is set, and both are additive - without a key
the system behaves exactly as before. Parsing is tested against captured
payloads so the suite never depends on either service being up.
"""

import pytest

from mcp_servers.places import provider as places_provider
from mcp_servers.transport import provider as flight_provider

# Captured from api.aviationstack.com, COK -> DEL.
AVIATIONSTACK_RESPONSE = {
    "pagination": {"total": 60},
    "data": [
        {
            "flight_date": "2026-09-05",
            "flight_status": "scheduled",
            "departure": {"iata": "COK", "scheduled": "2026-09-05T23:00:00+00:00"},
            "arrival": {"iata": "DEL", "scheduled": "2026-09-06T02:20:00+00:00"},
            "airline": {"name": "Air India Express"},
            "flight": {"iata": "IX1816"},
        },
        {
            "flight_date": "2026-09-05",
            "flight_status": "scheduled",
            "departure": {"iata": "COK", "scheduled": "2026-09-05T21:05:00+00:00"},
            "arrival": {"iata": "DEL", "scheduled": "2026-09-06T00:30:00+00:00"},
            "airline": {"name": "Air India"},
            "flight": {"iata": "AI1828"},
        },
        # The same flight appears again in a timetable.
        {
            "flight_date": "2026-09-04",
            "departure": {"iata": "COK", "scheduled": "2026-09-04T23:00:00+00:00"},
            "arrival": {"iata": "DEL", "scheduled": "2026-09-05T02:20:00+00:00"},
            "airline": {"name": "Air India Express"},
            "flight": {"iata": "IX1816"},
        },
        # Incomplete records happen and must not become half a flight.
        {"departure": {}, "arrival": {}, "airline": {}, "flight": {}},
    ],
}

TAVILY_RESPONSE = {
    "answer": "India Gate in Delhi is open daily and free to visit.",
    "results": [
        {
            "title": "India Gate",
            "url": "https://delhitourism.gov.in/india_gate.html",
            "content": "India Gate is a war memorial in New Delhi.",
        },
    ],
}


# --- AviationStack ----------------------------------------------------------


def test_real_airlines_and_flight_numbers_are_used():
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    assert {flight["id"] for flight in flights} == {"IX1816", "AI1828"}
    assert {flight["provider"] for flight in flights} == {"Air India Express", "Air India"}


def test_a_repeated_flight_number_appears_once():
    """A timetable lists the same flight every day; the traveller wants one row."""
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    assert len(flights) == len({flight["id"] for flight in flights})


def test_incomplete_records_are_dropped_not_half_used():
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    assert all(flight["provider"] and flight["duration_hours"] > 0 for flight in flights)


def test_the_duration_is_computed_from_the_real_times():
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    by_id = {flight["id"]: flight for flight in flights}
    assert by_id["IX1816"]["duration_hours"] == pytest.approx(3.3)  # 23:00 to 02:20
    assert by_id["IX1816"]["depart_time"] == "23:00"


def test_the_travellers_date_is_used_not_the_timetables():
    """The free plan only serves today, so the date has to come from the request."""
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    assert all(flight["date"] == "2026-09-19" for flight in flights)


def test_an_estimated_price_is_labelled_as_one():
    """The schedule is real and the fare is not; the record must never blur that."""
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    for flight in flights:
        assert flight["source"] == "aviationstack"
        assert flight["price_estimated"] is True
        assert flight["price_per_person"] > 0


def test_the_same_flight_is_quoted_the_same_price_twice():
    first = flight_provider.parse_flights(AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2)
    second = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2
    )
    assert first == second


def test_total_price_scales_with_passengers():
    flights = flight_provider.parse_flights(
        AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 3
    )
    assert all(f["total_price"] == f["price_per_person"] * 3 for f in flights)


def test_the_shape_matches_the_mock_provider():
    from mcp_servers.transport.mock_data import search_flights as mock_search

    real = flight_provider.parse_flights(AVIATIONSTACK_RESPONSE, "Kochi", "Delhi", "2026-09-19", 2)
    mock = mock_search("Kochi", "Delhi", "2026-09-19", 2)[0]
    assert set(mock) <= set(real[0])


def test_a_city_with_no_airport_returns_nothing_without_calling_out(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not have called out")

    monkeypatch.setattr(flight_provider.httpx, "get", explode)
    assert flight_provider.search_flights("Delhi", "Manali", "2026-09-19", 2, api_key="k") == []


def test_an_api_error_is_raised_not_silently_mocked(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": {"code": "usage_limit_reached", "message": "quota exhausted"}}

    monkeypatch.setattr(flight_provider.httpx, "get", lambda *a, **k: FakeResponse())

    with pytest.raises(flight_provider.FlightProviderError, match="quota exhausted"):
        flight_provider.search_flights("Kochi", "Delhi", "2026-09-19", 2, api_key="k")


# --- Tavily -----------------------------------------------------------------


def test_a_description_and_its_source_are_kept():
    described = places_provider.parse_description(TAVILY_RESPONSE)
    assert "India Gate" in described["about"]
    assert described["learn_more"].startswith("https://")
    assert described["described_by"] == "tavily"


def test_a_response_with_nothing_useful_is_none():
    assert places_provider.parse_description({"answer": "", "results": []}) is None


def test_descriptions_are_capped_so_a_plan_stays_readable():
    long_answer = {"answer": "x" * 5000, "results": []}
    assert len(places_provider.parse_description(long_answer)["about"]) <= 400


def test_only_the_first_few_places_are_described(monkeypatch):
    """The free tier is metered monthly; describing everything would exhaust it."""
    calls = []

    def fake_describe(name, city, api_key, timeout):
        calls.append(name)
        return {"about": "something", "learn_more": "https://x", "described_by": "tavily"}

    monkeypatch.setattr(places_provider, "describe_place", fake_describe)
    places = [{"name": f"Place {i}", "entry_fee": 0} for i in range(8)]

    enriched = places_provider.describe_places(places, "Delhi", "key")

    assert len(calls) == places_provider.MAX_DESCRIBED
    assert sum(1 for place in enriched if "about" in place) == places_provider.MAX_DESCRIBED
    assert len(enriched) == 8  # nothing is dropped


def test_a_failed_lookup_leaves_the_place_untouched(monkeypatch):
    monkeypatch.setattr(places_provider, "describe_place", lambda *a, **k: None)
    places = [{"name": "Red Fort", "entry_fee": 600}]

    assert places_provider.describe_places(places, "Delhi", "key") == places


def test_descriptions_never_change_the_numbers(monkeypatch):
    """Prose comes from the web; every figure stays ours."""

    def fake_describe(name, city, api_key, timeout):
        return {"about": "Entry costs 9999 rupees", "learn_more": None, "described_by": "tavily"}

    monkeypatch.setattr(places_provider, "describe_place", fake_describe)
    places = [{"name": "Red Fort", "entry_fee": 600, "visit_duration_hours": 3.0}]

    enriched = places_provider.describe_places(places, "Delhi", "key")

    assert enriched[0]["entry_fee"] == 600
    assert enriched[0]["visit_duration_hours"] == 3.0
