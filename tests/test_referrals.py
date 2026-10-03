from datetime import datetime

import pytest

import db
import seed
from app import create_app

NOW = datetime(2026, 10, 5, 9, 0)
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GOOD = {"patient_phone": "0712 345 678", "facility_id": "FAC-A", "service": "antenatal"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("REFERRAL_TOKEN", TOKEN)
    conn, sent = db.connect(), []
    app = create_app(auth_token="t", conn=conn, sender=lambda to, body: sent.append((to, body)) or "SM1",
                     now_fn=lambda: NOW)
    return app.test_client(), conn, sent


def post(client, body, headers=AUTH):
    return client.post("/api/referrals", json=body, headers=headers)


def test_creates_case_and_texts_patient(env):
    client, conn, sent = env
    r = post(client, GOOD)
    assert r.status_code == 201
    case = r.get_json()["case"]
    assert (case["id"], case["patient_phone"], case["status"], case["patient_area"]) == \
        ("R-0001", "+254712345678", "contacted", "Kayole")
    assert r.get_json()["intro_sent"] is True
    assert sent == [("+254712345678", "Hi! You were referred to Demo Mto Health Centre for antenatal. "
                                      "Is anything stopping you from going? Reply here.")]


def test_ids_continue_after_existing_cases(env):
    client, conn, _ = env
    seed.seed_history(conn, NOW)                      # R-0001..R-0040
    assert post(client, GOOD).get_json()["case"]["id"] == "R-0041"


@pytest.mark.parametrize("headers,status", [({}, 401), ({"Authorization": "Bearer nope"}, 401)])
def test_requires_token(env, headers, status):
    client, conn, sent = env
    assert post(client, GOOD, headers).status_code == status
    assert sent == [] and conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_endpoint_off_without_server_token(monkeypatch):
    monkeypatch.delenv("REFERRAL_TOKEN", raising=False)
    client = create_app(auth_token="t", conn=db.connect(), sender=lambda to, body: "x").test_client()
    assert post(client, GOOD, {"Authorization": "Bearer "}).status_code == 503


INVALID = [
    ("bad phone", {**GOOD, "patient_phone": "12345"}, "patient_phone"),
    ("landline-ish", {**GOOD, "patient_phone": "+254201234567"}, "patient_phone"),
    ("unknown facility", {**GOOD, "facility_id": "FAC-Z"}, "facility_id"),
    ("service not offered", {**GOOD, "service": "eye"}, "service"),
    ("unknown area", {**GOOD, "area": "Mombasa"}, "area"),
    ("not json", None, "patient_phone"),
]


@pytest.mark.parametrize("cid,body,field", INVALID, ids=[c[0] for c in INVALID])
def test_validation(env, cid, body, field):
    client, conn, sent = env
    r = post(client, body)
    assert r.status_code == 400 and field in r.get_json()["errors"]
    assert sent == []


def test_second_open_referral_for_same_phone_is_refused(env):
    client, conn, sent = env
    post(client, GOOD)
    r = post(client, {**GOOD, "patient_phone": "+254712345678"})
    assert r.status_code == 409 and len(sent) == 1


def test_failed_intro_still_creates_case(monkeypatch):
    from app import SendError
    monkeypatch.setenv("REFERRAL_TOKEN", TOKEN)

    def fail(to, body):
        raise SendError(21608)
    conn = db.connect()
    client = create_app(auth_token="t", conn=conn, sender=fail, now_fn=lambda: NOW).test_client()
    r = post(client, GOOD)
    assert r.status_code == 201 and r.get_json()["intro_sent"] is False
    assert [u["case_id"] for u in db.unreachable(conn)] == ["R-0001"]


def test_cors_preflight_allows_post_with_authorization(env):
    client, _, _ = env
    pre = client.options("/api/referrals", headers={"Origin": "https://x.lovable.app",
                                                   "Access-Control-Request-Method": "POST"})
    assert "POST" in pre.headers["Access-Control-Allow-Methods"]
    assert "Authorization" in pre.headers["Access-Control-Allow-Headers"]
    read = client.get("/api/dashboard")
    assert "POST" not in read.headers["Access-Control-Allow-Methods"]


def test_form_page_and_facilities_list(env):
    client, _, _ = env
    assert b"New referral" in client.get("/referrals/new").data
    assert {f["id"] for f in client.get("/api/facilities").get_json()} == {"FAC-A", "FAC-B", "FAC-C", "FAC-D"}
