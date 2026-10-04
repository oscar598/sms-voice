"""Transport directions fit one SMS and give up detail gracefully."""

import pytest

import directions
import sms


def route(seconds, rides=(), fare=None):
    r = {"duration": f"{seconds}s", "localizedValues": {}}
    if fare:
        r["localizedValues"]["transitFare"] = {"text": fare}
    if rides:
        r["legs"] = [{"steps": [{"travelMode": "WALK"}] + [
            {"travelMode": "TRANSIT", "transitDetails": {"transitLine": {
                "nameShort": name, "vehicle": {"name": {"text": vehicle}}}}} for vehicle, name in rides]}]
    return r


NEAR = {"DRIVE": route(420), "TRANSIT": route(1320, [("Bus", "M15")], "$3.00"), "BICYCLE": route(600)}
SINAI = ("Mount Sinai Hospital", "1 Gustave L. Levy Place, New York, NY 10029")


def test_near_clinic_gets_full_address_and_all_three_modes():
    out = directions.compose(*SINAI, NEAR)
    assert len(out) <= sms.limit()
    assert out.startswith(f"{SINAI[0]} is at {SINAI[1]}.")
    assert "7 min by car" in out and "the M15 bus" in out and "10 min by bike" in out


def test_bike_goes_before_the_full_address():
    long_rides = {**NEAR, "TRANSIT": route(3000, [("Subway", "6 Train"), ("Bus", "M101")], "$2.90")}
    out = directions.compose("NewYork-Presbyterian/Weill Cornell Medical Center",
                             "525 East 68th Street, New York, NY 10065", long_rides)
    assert len(out) <= sms.limit() and "by car" in out and "6 Train" in out
    assert "by bike" not in out and "525 East 68th Street, New York, NY 10065" in out


def test_long_names_still_keep_car_and_transit():
    many = {"DRIVE": route(4140), "TRANSIT": route(11000, [("Subway", "6 Train"), ("Subway", "N Line"),
                                                            ("Train", "LIRR"), ("Bus", "51")], "$12.25")}
    out = directions.compose("NewYork-Presbyterian/Columbia University Irving Medical Center",
                             "622 West 168th Street, New York, NY 10032", many)
    assert len(out) <= sms.limit() and "by car" in out and "6 Train" in out


def test_bike_over_an_hour_is_not_suggested():
    assert directions.phrase("BICYCLE", route(3601)) is None
    assert directions.phrase("BICYCLE", route(3600)) == "1 hr 0 min by bike"


def test_all_walking_transit_is_not_suggested():
    assert directions.phrase("TRANSIT", {"duration": "900s", "legs": [{"steps": [{"travelMode": "WALK"}]}]}) is None


def test_no_routes_still_gives_the_address():
    out = directions.compose("Bellevue Hospital", "462 First Avenue, New York, NY 10016", {})
    assert out == "Bellevue Hospital is at 462 First Avenue, New York, NY 10016."


@pytest.mark.parametrize("limit", [160, 120, 90])
def test_always_within_the_configured_limit(monkeypatch, limit):
    monkeypatch.setenv("SMS_MAX_CHARS", str(limit))
    assert len(directions.compose(*SINAI, NEAR)) <= limit
