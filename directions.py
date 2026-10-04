"""Car, public-transport and bicycle directions for the transport barrier.

Uses the Google Maps Routes API (computeRoutes) to find one driving route, one
transit route and, when the ride takes an hour or less, one cycling route from
the patient's location to the clinic, and turns them into one SMS of at most
SMS_MAX_CHARS. Code writes these facts, never the model, so a route can't be
invented. Any failure (no key, no address, API error, no route found) degrades
to "here is the clinic address" rather than raising.

Example (160 chars max; the help number follows in a second SMS):
  Mount Sinai Hospital is at 1 Gustave L. Levy Place, New York, NY 10029. It's
  7 min by car, 22 min on the M15 bus ($3.00) or 10 min by bike.
"""

import os
import re

import requests

import sms

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
TIMEOUT_S = 10
MAX_BIKE_SECONDS = 60 * 60  # longer rides are not suggested
MODES = ("DRIVE", "TRANSIT", "BICYCLE")
FIELD_MASK = ",".join([
    "routes.duration", "routes.localizedValues",
    "routes.legs.steps.travelMode", "routes.legs.steps.transitDetails",
])


def api_key():
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


class RouteError(Exception):
    pass


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
        r = post(ROUTES_URL, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK},
                 json=body, timeout=TIMEOUT_S)
        if not r.ok:
            raise RouteError(f"HTTP {r.status_code}: {r.text[:200]}")
        routes = r.json().get("routes") or []
    except (requests.RequestException, ValueError) as e:
        raise RouteError(str(e)) from e
    return routes[0] if routes else None


# ------------------------------------------------------------- short phrases


def _seconds(route):
    m = re.fullmatch(r"(\d+)s", str(route.get("duration") or ""))
    return int(m.group(1)) if m else None


def _time(route):
    """'7 min', '1 hr 9 min' (compact, for one SMS)."""
    secs = _seconds(route)
    if secs is None:
        return route.get("localizedValues", {}).get("duration", {}).get("text")
    mins = max(1, round(secs / 60))
    return f"{mins // 60} hr {mins % 60} min" if mins >= 60 else f"{mins} min"


def _ride_name(vehicle, name):
    """'the 6 Train', 'the N Line', 'the M15 bus', 'a matatu'."""
    if not name:
        return f"a {vehicle}"
    if any(w in name.lower() for w in ("train", "line", "lirr")):
        return f"the {name}"
    return f"the {name} {vehicle}"


def _rides(route):
    rides = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            t = step.get("transitDetails")
            if step.get("travelMode") == "TRANSIT" and t:
                line = t.get("transitLine", {})
                vehicle = (line.get("vehicle", {}).get("name", {}).get("text") or "bus").lower()
                rides.append(_ride_name(vehicle, line.get("nameShort") or line.get("name") or ""))
    return rides


def phrase(mode, route, detail=2):
    """One mode as a short phrase, or None if it shouldn't be suggested.

    detail 2: every ride and the fare; 1: the first ride; 0: just "by public transport".
    """
    t = _time(route)
    if not t:
        return None
    if mode == "DRIVE":
        return f"{t} by car"
    if mode == "BICYCLE":
        secs = _seconds(route)
        return f"{t} by bike" if secs is not None and secs <= MAX_BIKE_SECONDS else None
    rides = _rides(route)
    if not rides:
        return None  # a "transit" route that is all walking is not a transit suggestion
    if detail == 0:
        return f"{t} by public transport"
    if detail == 1:
        return f"{t} on {rides[0]}" + (" and more" if len(rides) > 1 else "")
    fare = route.get("localizedValues", {}).get("transitFare", {}).get("text")
    return f"{t} on " + " then ".join(rides) + (f" ({fare})" if fare else "")


def _join(parts):
    if not parts:
        return ""
    if len(parts) == 1:
        return f"It's {parts[0]}."
    return f"It's {', '.join(parts[:-1])} or {parts[-1]}."


def compose(clinic_name, clinic_address, routes):
    """The richest message that fits SMS_MAX_CHARS.

    The full address, the car and the bus matter most; the bike is the first
    thing given up, then transit detail, then the clinic name and the full
    address. The help number goes in its own SMS (core.help_sms).
    """
    short = clinic_address.split(",")[0].strip()
    heads = [f"{clinic_name} is at {clinic_address}.", f"{clinic_name} is at {short}.", f"We're at {short}."]
    bike, no_bike, car_only = [], ["BICYCLE"], ["BICYCLE", "TRANSIT"]
    tries = ([(bike, heads[0], d) for d in (2, 1)]
             + [(no_bike, h, d) for h in heads for d in (2, 1, 0)]
             + [(car_only, h, 0) for h in heads] + [(list(MODES), h, 0) for h in heads])
    for drop, head, detail in tries:
        parts = [p for m in MODES if m in routes and m not in drop for p in [phrase(m, routes[m], detail)] if p]
        text = sms.to_gsm(" ".join(x for x in (head, _join(parts)) if x))
        if len(text) <= sms.limit():
            return text
    return sms.fit(heads[-1])


def transport_sms(patient_address, clinic_name, clinic_address, key=None, post=requests.post, log=print):
    """One SMS: where the clinic is, then car / transit / bike (if an hour or less)."""
    key = key or api_key()
    routes = {}
    if not key or not patient_address:
        log("  directions skipped: " + ("GOOGLE_MAPS_API_KEY is not set" if not key else "patient has no address"))
    else:
        for mode in MODES:
            try:
                route = compute_route(patient_address, clinic_address, mode, key=key, post=post)
            except RouteError as e:
                log(f"  directions {mode} failed: {e}")
                continue
            if route and phrase(mode, route):
                routes[mode] = route
            elif route and mode == "BICYCLE":
                log(f"  directions: bicycle not suggested ({_time(route)} is over an hour)")
            else:
                log(f"  directions: no {mode} route found")
    return compose(clinic_name, clinic_address, routes)
