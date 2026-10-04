"""Car, public-transport and bicycle directions for the transport barrier (server.py).

Uses the Google Maps Routes API (computeRoutes) to find one driving route, one
transit route and, when the ride takes an hour or less, one cycling route from
the patient's address to the clinic, and turns them into plain SMS text. Code writes these facts, never the model, so a route can't be
invented. Any failure (no key, no address, API error, no route found) degrades
to "here is the clinic address" rather than raising.
"""

import os
import re

import requests

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
TIMEOUT_S = 10
MAX_SMS_CHARS = 480  # about three SMS segments
MAX_BIKE_SECONDS = 60 * 60  # longer rides are not suggested
FIELD_MASK = ",".join([
    "routes.duration", "routes.distanceMeters", "routes.description",
    "routes.localizedValues", "routes.legs.steps.travelMode",
    "routes.legs.steps.transitDetails",
])


def api_key():
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


def compute_route(origin, destination, mode, key=None, post=requests.post):
    """Return the first route dict for `mode` ('DRIVE', 'TRANSIT', 'BICYCLE'), or None."""
    key = key or api_key()
    if not (key and origin and destination):
        return None
    body = {"origin": {"address": origin}, "destination": {"address": destination},
            "travelMode": mode, "languageCode": "en", "units": "METRIC"}
    if mode == "DRIVE":
        # Traffic-aware times for driving only: the API rejects routingPreference
        # for TRANSIT, WALK and BICYCLE with a 400.
        body["routingPreference"] = "TRAFFIC_AWARE"
    try:
        r = post(
            ROUTES_URL,
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK},
            json=body,
            timeout=TIMEOUT_S,
        )
        if not r.ok:
            raise RouteError(f"HTTP {r.status_code}: {r.text[:200]}")
        routes = r.json().get("routes") or []
    except (requests.RequestException, ValueError) as e:
        raise RouteError(str(e)) from e
    return routes[0] if routes else None


class RouteError(Exception):
    pass


def _seconds(duration):
    """'1234s' -> 1234, or None."""
    m = re.fullmatch(r"(\d+)s", str(duration or ""))
    return int(m.group(1)) if m else None


def _minutes(duration):
    """'1234s' -> '21 min'."""
    secs = _seconds(duration)
    if secs is None:
        return None
    mins = max(1, round(secs / 60))
    return f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"


def _duration(route):
    return (route.get("localizedValues", {}).get("duration", {}).get("text")
            or _minutes(route.get("duration")))


def _distance(route):
    text = route.get("localizedValues", {}).get("distance", {}).get("text")
    if text:
        return text
    meters = route.get("distanceMeters")
    return f"{meters / 1000:.1f} km" if meters else None


def _trip(route):
    """'about 7 mins (2.7 km)', whichever parts are known."""
    dur, dist = _duration(route), _distance(route)
    return " ".join(p for p in (f"about {dur}" if dur else None, f"({dist})" if dist else None) if p)


def _via(route):
    return f" along {route['description']}" if route.get("description") else ""


def describe_drive(route):
    return f"Driving takes {_trip(route) or 'a short time'}{_via(route)}."


def _ride_name(vehicle, name):
    """'the 6 Train', 'the N Line', 'the 51 bus', 'matatu 46'."""
    if not name:
        return f"a {vehicle}"
    if any(w in name.lower() for w in ("train", "line", "lirr")):
        return f"the {name}"
    return f"the {name} {vehicle}"


MAX_RIDES_SHOWN = 3  # longer trips list the first rides and the last one


def describe_transit(route, max_rides=MAX_RIDES_SHOWN):
    rides = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            t = step.get("transitDetails")
            if step.get("travelMode") != "TRANSIT" or not t:
                continue
            line = t.get("transitLine", {})
            vehicle = (line.get("vehicle", {}).get("name", {}).get("text") or "bus").lower()
            name = line.get("nameShort") or line.get("name") or ""
            stops = t.get("stopDetails", {})
            frm = stops.get("departureStop", {}).get("name")
            to = stops.get("arrivalStop", {}).get("name")
            ride = _ride_name(vehicle, name)
            if frm:
                ride += f" from {frm}"
            if to:
                ride += f" to {to}"
            rides.append(ride)
    if not rides:
        return None  # a "transit" route that is all walking is not a transit suggestion
    dur = _duration(route)
    fare = route.get("localizedValues", {}).get("transitFare", {}).get("text")
    head = f"By public transport it's about {dur}" if dur else "By public transport"
    if fare:
        head += f" and costs {fare}"
    if max_rides == 1 and len(rides) > 1:
        return f"{head}, starting with {rides[0]}."  # shortest form, for long trips
    if len(rides) > max_rides:
        rides = rides[: max_rides - 1] + [f"a few more connections, and finally {rides[-1]}"]
    return f"{head}: take " + ", then ".join(rides) + "."


def describe_bike(route):
    secs = _seconds(route.get("duration"))
    if secs is None or secs > MAX_BIKE_SECONDS:
        return None  # over an hour by bike (or unknown) is not a sensible suggestion
    return f"If you'd rather cycle, it's {_trip(route)}{_via(route)}."


def transport_sms(patient_address, clinic_name, clinic_address, key=None, post=requests.post,
                  log=print, footer=""):
    """SMS text: clinic address, then car, transit and (if an hour or less) bicycle routes.

    `footer` (e.g. the transportation-assistance line) always survives the length
    cap; the routes are shortened instead.
    """
    address_only = f"We're at {clinic_name}, {clinic_address}."
    key = key or api_key()
    if not key or not patient_address:
        log("  directions skipped: " + ("GOOGLE_MAPS_API_KEY is not set" if not key
                                        else "patient has no address"))
        return "\n".join(filter(None, [address_only, footer]))
    found = {}  # mode -> [line text, route]
    for mode, describe in (("DRIVE", describe_drive), ("TRANSIT", describe_transit),
                           ("BICYCLE", describe_bike)):
        try:
            route = compute_route(patient_address, clinic_address, mode, key=key, post=post)
        except RouteError as e:
            log(f"  directions {mode} failed: {e}")
            continue
        text = describe(route) if route else None
        if text:
            found[mode] = [text, route]
        elif route and mode == "BICYCLE":
            log(f"  directions: bicycle not suggested ({_duration(route)} is over an hour)")
        else:
            log(f"  directions: no {mode} route found")
    head = f"Here's how to get to {clinic_name} at {clinic_address}:" if found else address_only
    room = MAX_SMS_CHARS - (len(footer) + 1 if footer else 0)

    def render():
        return "\n".join([head] + [text for text, _ in found.values()])

    # Over the limit: first shorten a long public-transport trip (first and last ride,
    # then just the first ride), then drop whole suggestions from the end (bike, then transit), and only as a
    # last resort cut mid-sentence, so the message still reads like a person wrote it.
    for max_rides in (2, 1):
        if len(render()) > room and "TRANSIT" in found:
            found["TRANSIT"][0] = describe_transit(found["TRANSIT"][1], max_rides=max_rides)
    while len(render()) > room and len(found) > 1:
        found.pop(list(found)[-1])
    sms = render()
    if len(sms) > room:
        sms = sms[: room - 3] + "..."
    return "\n".join(filter(None, [sms, footer]))
