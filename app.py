"""Plane Spotter: which plane is flying over a place right now.

One question in, one aircraft out. The chain is three public, keyless calls:

1. OpenStreetMap's Nominatim turns the place the user named into a latitude
   and longitude (UK first, then anywhere, so "Richmond" means the London one).
2. adsb.lol lists every aircraft its volunteer receivers can hear within a
   radius of that point. We keep only the ones in the air and pick the nearest.
3. adsbdb turns that aircraft's callsign into a route and an airline. Many
   flights (private planes, police helicopters) have no published route, and
   that is fine: those fields are simply left out.

The app keeps nothing between calls and is told nothing about who asked.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: Nominatim answers 403 without a descriptive User-Agent.
USER_AGENT = "pippa-agent-app-plane-spotter/0.1 (https://github.com/Pippa-Labs)"

NOMINATIM = "https://nominatim.openstreetmap.org/search?"
ADSB_POINT = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius}"
ADSBDB_CALLSIGN = "https://api.adsbdb.com/v0/callsign/"

#: Search radius in nautical miles (adsb.lol's unit). 10 nm is about 11.5
#: miles: far enough to catch the plane you can see, close enough to be "over".
RADIUS_NM = 10

#: Greater London, as Nominatim's lon1,lat1,lon2,lat2. Used to rank London
#: matches first for an ambiguous name; it never excludes anywhere else.
LONDON_VIEWBOX = "-0.51,51.69,0.33,51.28"

#: Nominatim importance above which a UK-wide match is trusted as-is. Cities
#: and well-known districts score 0.4-0.8; a stray footpath scores ~0.04.
WELL_KNOWN = 0.3

NM_TO_MILES = 1.15078
NM_TO_FEET = 6076.12
KNOTS_TO_MPH = 1.15078

#: Friendly names for the ICAO type codes seen most over the UK. Anything not
#: listed is shown as its code, which is still a true answer.
AIRCRAFT_TYPES = {
    "A19N": "Airbus A319neo", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
    "A318": "Airbus A318", "A319": "Airbus A319", "A320": "Airbus A320",
    "A321": "Airbus A321", "A332": "Airbus A330-200", "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo", "A339": "Airbus A330-900neo",
    "A343": "Airbus A340-300", "A346": "Airbus A340-600",
    "A359": "Airbus A350-900", "A35K": "Airbus A350-1000", "A388": "Airbus A380",
    "BCS1": "Airbus A220-100", "BCS3": "Airbus A220-300",
    "B737": "Boeing 737-700", "B738": "Boeing 737-800", "B739": "Boeing 737-900",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
    "B744": "Boeing 747-400", "B748": "Boeing 747-8", "B752": "Boeing 757-200",
    "B763": "Boeing 767-300", "B764": "Boeing 767-400",
    "B772": "Boeing 777-200", "B77L": "Boeing 777-200LR", "B77W": "Boeing 777-300ER",
    "B788": "Boeing 787-8 Dreamliner", "B789": "Boeing 787-9 Dreamliner",
    "B78X": "Boeing 787-10 Dreamliner",
    "E170": "Embraer E170", "E175": "Embraer E175", "E190": "Embraer E190",
    "E195": "Embraer E195", "E290": "Embraer E190-E2", "E295": "Embraer E195-E2",
    "AT72": "ATR 72", "AT76": "ATR 72-600", "DH8D": "De Havilland Dash 8-400",
    "CRJ9": "Bombardier CRJ900", "RJ85": "Avro RJ85",
    "GL7T": "Bombardier Global 7500", "GLEX": "Bombardier Global Express",
    "C68A": "Cessna Citation Latitude", "C172": "Cessna 172",
    "PC12": "Pilatus PC-12", "P28A": "Piper Cherokee", "PA28": "Piper Cherokee",
    "SR22": "Cirrus SR22", "DA40": "Diamond DA40", "DA42": "Diamond DA42",
    "C152": "Cessna 152", "C182": "Cessna 182", "C25A": "Cessna Citation CJ2",
    "C56X": "Cessna Citation Excel", "E55P": "Embraer Phenom 300",
    "F2TH": "Dassault Falcon 2000", "FA7X": "Dassault Falcon 7X",
    "G280": "Gulfstream G280", "GLF6": "Gulfstream G650",
    "A400": "Airbus A400M Atlas", "C17": "Boeing C-17 Globemaster",
    "A306": "Airbus A300-600", "B77F": "Boeing 777 Freighter",
    "AS65": "Airbus Dauphin helicopter", "S92": "Sikorsky S-92 helicopter",
    "A109": "Leonardo AW109 helicopter", "A139": "Leonardo AW139 helicopter",
    "A169": "Leonardo AW169 helicopter", "AS50": "Airbus H125 helicopter",
    "EC35": "Airbus H135 helicopter", "EC45": "Airbus H145 helicopter",
    "B505": "Bell 505 helicopter", "R44": "Robinson R44 helicopter",
}

#: Typical seats in a two-class layout, so "about N people on board" is an
#: honest estimate. Freighters, business jets and helicopters are left out:
#: a number for those would be a guess dressed up as a fact.
TYPICAL_SEATS = {
    "A318": 110, "A319": 140, "A19N": 140, "A320": 180, "A20N": 186,
    "A321": 200, "A21N": 220, "A332": 250, "A333": 290, "A338": 250,
    "A339": 290, "A343": 270, "A346": 320, "A359": 320, "A35K": 350,
    "A388": 500, "BCS1": 110, "BCS3": 140,
    "B737": 140, "B738": 180, "B739": 190, "B38M": 180, "B39M": 190,
    "B744": 400, "B748": 410, "B752": 200, "B763": 250, "B764": 240,
    "B772": 300, "B77L": 300, "B77W": 350,
    "B788": 240, "B789": 280, "B78X": 320,
    "E170": 76, "E175": 80, "E190": 100, "E195": 120, "E290": 100, "E295": 130,
    "AT72": 70, "AT76": 70, "DH8D": 78, "CRJ9": 90, "RJ85": 85,
}

#: Below this a plane is climbing out of or coming into an airport near the
#: place, so one end of its route must be within NEAR_AIRPORT_MILES of it.
LOW_ALTITUDE_FEET = 10_000
NEAR_AIRPORT_MILES = 60

#: An airline flight's callsign: the airline's three-letter ICAO code, then
#: a digit. A private plane's callsign is usually its registration (GOHLI).
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,4}$")

COMPASS = ["north", "north-east", "east", "south-east",
           "south", "south-west", "west", "north-west"]


class NoPlaceGiven(AgentAppError):
    user_message = (
        "Tell me where to look, for example "
        "\"what plane is flying over Hammersmith\" or a postcode like SW7 2AZ."
    )
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place on the map."
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── the one network seam, replaced in tests ─────────────────────────────

    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _get_json(self, url: str, service: str, timeout: float):
        try:
            raw = self._fetch(url, timeout=timeout)
        except urllib.error.HTTPError as exc:  # must come before URLError
            if exc.code == 429:
                raise RateLimitedError(f"{service} rate limited")
            raise ServiceUnavailableError(f"{service} returned {exc.code}")
        except TimeoutError:
            raise ServiceUnavailableError(f"{service} timed out")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"could not reach {service}: {exc.reason}")
        try:
            return json.loads(raw)
        except ValueError:
            raise ServiceUnavailableError(f"{service} sent something that is not JSON")

    # ── the request ─────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "plane.overhead":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = (call.inputs.get("place") or "").strip()
        if not place:
            raise NoPlaceGiven("no place in the message")

        lat, lon, place_name = self._locate(place)
        aircraft = self._aircraft_near(lat, lon)

        if not aircraft:
            return Result(
                data={"place": place_name, "plane_found": False, "planes_nearby": 0},
                narrative=f"No aircraft are in the air within about 12 miles of "
                          f"{place_name} right now.",
            )

        data: dict = {
            "place": place_name,
            "plane_found": True,
            "planes_nearby": len(aircraft),
        }
        data.update(self._describe(self._pick(aircraft)))
        route = self._route(data.get("callsign"))
        ends = route.pop("_ends", [])
        if data["altitude_feet"] < LOW_ALTITUDE_FEET and ends and not any(
            miles_between(lat, lon, a, b) <= NEAR_AIRPORT_MILES for a, b in ends
        ):
            # Low over here but neither airport is nearby: the route on file
            # for this callsign is stale. Saying nothing beats a wrong city.
            route = {k: v for k, v in route.items()
                     if k not in ("origin", "destination")}
        data.update(route)
        return Result(data=data, narrative=self._sentence(data))

    def _locate(self, place: str) -> tuple[float, float, str]:
        """Place name or postcode -> (lat, lon, tidy name).

        Three tries, stopping at the first good one:

        1. Anywhere in the UK, taken only if it is a well-known place
           (Nominatim's `importance`), so "Manchester" is the city.
        2. The UK ranked London-first, so a minor name like "Big Ben" is the
           clock, not the cycleway in the Highlands that tries 1 finds.
        3. Anywhere in the world.
        """
        tries = (
            ({"countrycodes": "gb"}, WELL_KNOWN),
            ({"countrycodes": "gb", "viewbox": LONDON_VIEWBOX}, 0.0),
            ({}, 0.0),
        )
        for extra, min_importance in tries:
            query = {"q": place, "format": "json", "limit": 1, **extra}
            results = self._get_json(NOMINATIM + urlencode(query), "the map service", 4.0)
            if not (isinstance(results, list) and results and isinstance(results[0], dict)):
                continue
            hit = results[0]
            try:
                lat, lon = float(hit["lat"]), float(hit["lon"])
                importance = float(hit.get("importance") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if importance < min_importance:
                continue
            name = hit.get("name") or str(hit.get("display_name", "")).split(",")[0]
            return lat, lon, name or place
        error = PlaceNotFound(f"nominatim found nothing for {place!r}")
        error.user_message = f"I could not find anywhere called {place} on the map."
        raise error

    def _aircraft_near(self, lat: float, lon: float) -> list[dict]:
        """Airborne aircraft within the radius, nearest first."""
        url = ADSB_POINT.format(lat=lat, lon=lon, radius=RADIUS_NM)
        payload = self._get_json(url, "the flight tracker", 6.0)
        planes = payload.get("ac") if isinstance(payload, dict) else None
        airborne = []
        for plane in planes or []:
            if not isinstance(plane, dict):
                continue
            altitude = plane.get("alt_baro")
            if isinstance(altitude, bool) or not isinstance(altitude, (int, float)):
                continue  # "ground" or missing
            if altitude <= 0 or not isinstance(plane.get("dst"), (int, float)):
                continue
            airborne.append(plane)
        airborne.sort(key=lambda p: p["dst"])
        return airborne

    @staticmethod
    def _pick(aircraft: list[dict]) -> dict:
        """The nearest airline flight, else the nearest aircraft of any kind.

        The nearest thing in the sky over London is often a helicopter or a
        private plane flying under its registration (GOHLI), which has no
        route. The airliner a person is pointing at has an airline callsign
        (three letters then a digit: BAW35, EZY81QJ), and a route to report.
        """
        for plane in aircraft:  # already nearest first
            if AIRLINE_CALLSIGN.match(str(plane.get("flight") or "").strip()):
                return plane
        return aircraft[0]

    @staticmethod
    def _describe(plane: dict) -> dict:
        out: dict = {}
        callsign = str(plane.get("flight") or "").strip()
        if callsign:
            out["callsign"] = callsign
        code = str(plane.get("t") or "").strip().upper()
        if code:
            out["aircraft_type"] = AIRCRAFT_TYPES.get(code, code)
        registration = str(plane.get("r") or "").strip()
        if registration:
            out["registration"] = registration
        out["altitude_feet"] = int(plane["alt_baro"])
        speed = plane.get("gs")
        if isinstance(speed, (int, float)):
            out["speed_mph"] = int(round(speed * KNOTS_TO_MPH))
        out["distance_miles"] = round(float(plane["dst"]) * NM_TO_MILES, 1)
        bearing = plane.get("dir")
        if isinstance(bearing, (int, float)):
            out["direction"] = COMPASS[int((bearing % 360 + 22.5) // 45) % 8]
        # How high to look: the angle up from the horizon, from the height
        # and the ground distance. Flat-earth is fine within 12 miles.
        ground_feet = float(plane["dst"]) * NM_TO_FEET
        angle = 90 if ground_feet <= 0 else round(
            math.degrees(math.atan2(out["altitude_feet"], ground_feet))
        )
        out["look_up_degrees"] = int(angle)
        out["where_to_look"] = _where_to_look(out.get("direction"), angle)
        seats = TYPICAL_SEATS.get(code)
        if seats:
            out["people_on_board"] = seats
        rate = plane.get("baro_rate", plane.get("geom_rate"))
        if isinstance(rate, (int, float)):
            out["climbing_or_descending"] = (
                "climbing" if rate > 300 else "descending" if rate < -300 else "level"
            )
        return out

    def _route(self, callsign: str | None) -> dict:
        """Route and airline for a callsign. Best effort: never fails the call."""
        if not callsign:
            return {}
        try:
            payload = self._get_json(
                ADSBDB_CALLSIGN + quote(callsign, safe=""), "the route service", 4.0
            )
        except AgentAppError:
            return {}  # the plane is still worth reporting without its route
        route = payload.get("response") if isinstance(payload, dict) else None
        route = route.get("flightroute") if isinstance(route, dict) else None
        if not isinstance(route, dict):
            return {}  # "unknown callsign": private, military, police...
        out: dict = {}
        if route.get("callsign_iata"):
            out["flight_number"] = str(route["callsign_iata"])
        airline = route.get("airline")
        if isinstance(airline, dict) and airline.get("name"):
            out["airline"] = str(airline["name"])
        ends = []
        for key in ("origin", "destination"):
            airport = route.get(key)
            if isinstance(airport, dict):
                try:
                    ends.append((float(airport["latitude"]), float(airport["longitude"])))
                except (KeyError, TypeError, ValueError):
                    pass
            if isinstance(airport, dict) and airport.get("name"):
                city = airport.get("municipality")
                name = str(airport["name"])
                out[key] = f"{name} ({city})" if city and city not in name else name
        if ends:
            out["_ends"] = ends  # internal: removed before returning
        return out

    @staticmethod
    def _sentence(d: dict) -> str:
        what = d.get("aircraft_type")
        kind = f", {_article(what)} {what}," if what else ""
        who = d.get("flight_number") or d.get("callsign")
        if d.get("airline") and who:
            lead = f"{d['airline']} flight {who}{kind}"
        elif who:
            lead = f"{who}{kind}"
        elif what:
            lead = f"{_article(what).capitalize()} {what}"
        else:
            lead = "An aircraft"
        where = f"{d['distance_miles']} miles"
        if d.get("direction"):
            where += f" {d['direction']} of {d['place']}"
        else:
            where += f" from {d['place']}"
        sentence = f"Look {d['where_to_look']}: {lead} is {where} at {d['altitude_feet']:,} feet"
        if d.get("people_on_board"):
            sentence += f", with about {d['people_on_board']} people on board"
        if d.get("origin") and d.get("destination"):
            sentence += f", flying from {d['origin']} to {d['destination']}"
        sentence += "."
        if d["planes_nearby"] > 1:
            sentence += f" {d['planes_nearby']} aircraft are in the air nearby."
        return sentence


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _where_to_look(direction: str | None, angle: int) -> str:
    """'south-west, about 25° up (low in the sky)' — words a person can point with."""
    if angle >= 75:
        return "almost straight up"
    if angle >= 45:
        height = "high in the sky"
    elif angle >= 20:
        height = "about halfway up the sky" if angle >= 35 else "low in the sky"
    else:
        height = "just above the horizon"
    towards = f"{direction}, " if direction else ""
    return f"{towards}about {angle}° up ({height})"


def _article(word: str) -> str:
    return "an" if word[:1].upper() in "AEIOU" else "a"
