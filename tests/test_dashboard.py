from datetime import datetime, timedelta

import pytest

import db
import metrics
import scheduler
import seed
from app import create_app

NOW = datetime(2026, 10, 5, 12, 0)


@pytest.fixture
def conn():
    c = db.connect()
    seed.seed_history(c, NOW)
    return c


def test_radar_flags_clinic_b_as_6_of_11(conn):
    radar = metrics.dashboard(conn, NOW, seed.FACILITIES)["radar"]["facilities"]
    top = radar[0]
    assert (top["facility_id"], top["flagged"], top["n"], top["hidden"]) == ("FAC-B", 6, 11, False)
    assert all(r["share"] < top["share"] for r in radar[1:])


def test_radar_window_and_min_n(conn):
    later = metrics.dashboard(conn, NOW + timedelta(days=7), seed.FACILITIES)["radar"]["facilities"]
    b = next(r for r in later if r["facility_id"] == "FAC-B")
    assert b["n"] < 5 and b["hidden"]          # old activity left the 7-day window
    assert later[-1]["hidden"]                 # hidden rows sort last


def test_radar_counts_barrier_at_original_facility_after_reroute(conn):
    db.update_case(conn, "R-0001", facility_id="FAC-D")  # rerouted after reporting FAC-B closed
    b = next(r for r in metrics.dashboard(conn, NOW, seed.FACILITIES)["radar"]["facilities"]
             if r["facility_id"] == "FAC-B")
    assert b["flagged"] == 6


def test_totals_and_rates(conn):
    d = metrics.dashboard(conn, NOW, seed.FACILITIES)
    t = d["totals"]
    assert t["cases"] == 40
    # Hand count of seed._HISTORY: completed B 3, A 7, C 3, D 5 = 18 (14 clinic, 4 patient).
    assert (t["completed"], t["completed_by_clinic"], t["completed_by_patient"]) == (18, 14, 4)
    assert t["lost"] == 1 and t["escalated_now"] == 4
    assert t["unresolved"] == 40 - 18 - 1
    assert d["completion_rate"] == round(18 / 40, 3)
    assert d["share_resolved_without_chw"] == round(1 - 4 / 40, 3)
    assert 0 < d["median_days_to_completion"] <= 5
    assert d["top_barriers"][0] == {"barrier": "transport", "cases": 8}


def test_seeded_cases_are_never_texted(conn):
    sent = []
    for days in range(1, 15):
        scheduler.tick(conn, lambda to, body: sent.append(to) or "x", NOW + timedelta(days=days), seed)
    assert sent == []
    assert all(c["patient_phone"].startswith("+2540000") for c in map(dict, conn.execute("SELECT * FROM cases")))


def test_endpoint_and_cors(conn):
    client = create_app(auth_token="t", conn=conn, sender=lambda to, body: "x", now_fn=lambda: NOW).test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and r.get_json()["totals"]["cases"] == 40
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    pre = client.options("/api/dashboard", headers={"Origin": "https://x.lovable.app",
                                                   "Access-Control-Request-Method": "GET"})
    assert "ngrok-skip-browser-warning" in pre.headers["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Origin" not in client.post("/sms").headers  # not /api: no CORS
