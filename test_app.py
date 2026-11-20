"""Tests for Plane Spotter. The build runs these; nothing here touches the network.

Every test replaces `App._fetch`, the one method that does HTTP, with a stub
that answers by URL. `conftest.py` blocks real sockets, so a test that forgot
to stub would fail loudly rather than pass on the network.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import (
    LocalRunner,
    OutputShapeViolation,
    RateLimitedError,
    ServiceUnavailableError,
)

from app import App, NoPlaceGiven, PlaceNotFound

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

# Real shapes, captured from the live services on 2026-11-20 and trimmed.
HAMMERSMITH = [{"lat": "51.4920377", "lon": "-0.2236401", "name": "Hammersmith",
                "display_name": "Hammersmith, London Borough of Hammersmith and Fulham"}]

AIRCRAFT = {"ac": [
    {"hex": "4009c6", "flight": "SHT12Y  ", "r": "G-EUUI", "t": "A320",
     "alt_baro": "ground", "gs": 0.0, "dst": 8.1, "dir": 264.5},
    {"hex": "406d7a", "flight": "BAW35   ", "r": "G-STBA", "t": "B77W",
     "alt_baro": 3800, "gs": 160.0, "baro_rate": -768, "dst": 2.4, "dir": 226.5},
    {"hex": "400abc", "flight": "GWOFM   ", "r": "G-WOFM", "t": "A109",
     "alt_baro": 1300, "gs": 50.0, "dst": 3.1, "dir": 128.5},
], "msg": "No error", "total": 3}

ROUTE = {"response": {"flightroute": {
    "callsign": "BAW35", "callsign_iata": "BA35",
    "airline": {"name": "British Airways"},
    "origin": {"name": "London Heathrow Airport", "municipality": "London"},
    "destination": {"name": "Chennai International Airport", "municipality": "Chennai"},
}}}


def stub(geocode=HAMMERSMITH, aircraft=AIRCRAFT, route=ROUTE, fail=None):
    """A fake `_fetch` that answers each service by its URL.

    `fail` maps a host fragment to an exception to raise for that service.
    """
    def fetch(url, timeout=6.0):
        for host, error in (fail or {}).items():
            if host in url:
                raise error
        if "nominatim" in url:
            return json.dumps(geocode).encode()
        if "adsb.lol" in url:
            return json.dumps(aircraft).encode()
        if "adsbdb" in url:
            return json.dumps(route).encode()
        raise AssertionError(f"unexpected url {url}")
    return fetch


def http_error(code):
    return urllib.error.HTTPError("https://example.com", code, "x", {}, io.BytesIO(b""))


def call(inputs, fetch=None, *, explicit=False):
    app = App()
    app._fetch = fetch or stub()
    return LocalRunner(app).call(
        "plane.overhead", inputs, granted_secrets={}, now=NOW,
        request_id="test-1", explicit=explicit,
    ).result


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_7065"
    assert [o.id for o in app.manifest.outcomes] == ["plane.overhead"]


def test_it_names_the_nearest_airborne_plane_with_its_route():
    data = call({"place": "Hammersmith"}).data
    assert data["place"] == "Hammersmith"
    assert data["plane_found"] is True
    assert data["planes_nearby"] == 2          # the plane on the ground is ignored
    assert data["callsign"] == "BAW35"
    assert data["flight_number"] == "BA35"
    assert data["airline"] == "British Airways"
    assert data["aircraft_type"] == "Boeing 777-300ER"
    # The city is left off when the airport's name already says it.
    assert data["origin"] == "London Heathrow Airport"
    assert data["destination"] == "Chennai International Airport"
    assert data["altitude_feet"] == 3800
    assert data["speed_mph"] == 184
    assert data["distance_miles"] == 2.8
    assert data["direction"] == "south-west"
    assert data["climbing_or_descending"] == "descending"


def test_the_sentence_reads_well():
    narrative = call({"place": "Hammersmith"}).narrative
    assert narrative.startswith("British Airways flight BA35, a Boeing 777-300ER,")
    assert "south-west of Hammersmith at 3,800 feet" in narrative
    assert "to Chennai International Airport." in narrative


def test_the_city_is_added_when_the_airport_name_lacks_it():
    route = {"response": {"flightroute": {
        "callsign_iata": "AH2055", "airline": {"name": "Air Algerie"},
        "origin": {"name": "London Heathrow Airport", "municipality": "London"},
        "destination": {"name": "Houari Boumediene Airport", "municipality": "Algiers"},
    }}}
    data = call({"place": "Hammersmith"}, stub(route=route)).data
    assert data["destination"] == "Houari Boumediene Airport (Algiers)"


def test_an_airliner_is_preferred_over_a_nearer_helicopter():
    """The helicopter is closer, but the airliner is what people point at."""
    sky = {"ac": [
        {"flight": "GOHLI   ", "t": "R44", "alt_baro": 1100, "dst": 1.0, "dir": 270},
        {"flight": "BAW35   ", "t": "B77W", "alt_baro": 3800, "dst": 4.0, "dir": 90},
    ]}
    data = call({"place": "Hammersmith"}, stub(aircraft=sky)).data
    assert data["callsign"] == "BAW35"
    assert data["planes_nearby"] == 2


def test_a_stale_route_is_dropped_for_a_low_plane():
    """3,800 ft over London cannot be Doha to Brisbane: say nothing instead."""
    stale = {"response": {"flightroute": {
        "callsign_iata": "QR4", "airline": {"name": "Qatar Airways"},
        "origin": {"name": "Hamad International Airport", "municipality": "Doha",
                   "latitude": 25.27, "longitude": 51.61},
        "destination": {"name": "Brisbane International Airport",
                        "municipality": "Brisbane",
                        "latitude": -27.38, "longitude": 153.12},
    }}}
    data = call({"place": "Hammersmith"}, stub(route=stale)).data
    assert data["airline"] == "Qatar Airways"
    assert "origin" not in data and "destination" not in data


def test_a_route_through_a_nearby_airport_is_kept():
    real = {"response": {"flightroute": {
        "callsign_iata": "BA35", "airline": {"name": "British Airways"},
        "origin": {"name": "London Heathrow Airport", "municipality": "London",
                   "latitude": 51.47, "longitude": -0.46},
        "destination": {"name": "Chennai International Airport",
                        "municipality": "Chennai",
                        "latitude": 12.99, "longitude": 80.17},
    }}}
    data = call({"place": "Hammersmith"}, stub(route=real)).data
    assert data["destination"] == "Chennai International Airport"


def test_with_no_airliner_the_nearest_aircraft_is_named():
    sky = {"ac": [
        {"flight": "GOHLI   ", "t": "R44", "alt_baro": 1100, "dst": 1.0, "dir": 270},
    ]}
    data = call({"place": "Hammersmith"},
                stub(aircraft=sky, route={"response": "unknown callsign"})).data
    assert data["callsign"] == "GOHLI"
    assert data["aircraft_type"] == "Robinson R44 helicopter"


def test_a_flight_with_no_published_route_still_answers():
    """Police helicopters and private planes have no route. Leave it out."""
    data = call({"place": "Hammersmith"},
                stub(route={"response": "unknown callsign"})).data
    assert data["callsign"] == "BAW35"
    assert "origin" not in data and "airline" not in data


def test_the_route_service_failing_does_not_lose_the_plane():
    data = call({"place": "Hammersmith"},
                stub(fail={"adsbdb": TimeoutError()})).data
    assert data["aircraft_type"] == "Boeing 777-300ER"
    assert "destination" not in data


def test_an_empty_sky_is_an_answer_not_an_error():
    result = call({"place": "Hammersmith"}, stub(aircraft={"ac": []}))
    assert result.data == {"place": "Hammersmith", "plane_found": False,
                           "planes_nearby": 0}
    assert "No aircraft" in result.narrative


def test_a_missing_aircraft_list_is_an_empty_sky():
    result = call({"place": "Hammersmith"}, stub(aircraft={"msg": "No error"}))
    assert result.data["plane_found"] is False


def test_no_place_says_what_to_type():
    with pytest.raises(NoPlaceGiven) as failure:
        call({"place": ""})
    assert "Hammersmith" in failure.value.user_message


def test_no_place_at_all_says_what_to_type():
    with pytest.raises(NoPlaceGiven):
        call({})


def test_a_place_that_does_not_exist_is_named():
    """Nominatim answers 200 with [] for a place that is not there."""
    with pytest.raises(PlaceNotFound) as failure:
        call({"place": "Narnia"}, stub(geocode=[]))
    assert failure.value.user_message == "I could not find anywhere called Narnia on the map."


def test_rate_limited_flight_tracker_says_try_again():
    with pytest.raises(RateLimitedError):
        call({"place": "Hammersmith"}, stub(fail={"adsb.lol": http_error(429)}))


def test_flight_tracker_down_is_retryable():
    with pytest.raises(ServiceUnavailableError):
        call({"place": "Hammersmith"}, stub(fail={"adsb.lol": http_error(503)}))


def test_a_timeout_is_caught():
    with pytest.raises(ServiceUnavailableError):
        call({"place": "Hammersmith"}, stub(fail={"adsb.lol": TimeoutError()}))


def test_unreachable_map_service_is_caught():
    with pytest.raises(ServiceUnavailableError):
        call({"place": "Hammersmith"},
             stub(fail={"nominatim": urllib.error.URLError("dns")}))


def test_picked_by_name_it_answers_the_same():
    assert call({"place": "Hammersmith"}, explicit=True).data == \
        call({"place": "Hammersmith"}).data


def test_the_runner_enforces_the_manifest(monkeypatch):
    from pippa_agentapp_sdk import Result

    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": 1}))
    with pytest.raises(OutputShapeViolation):
        call({"place": "Hammersmith"})
