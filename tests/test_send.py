from datetime import datetime

import pytest
from twilio.base.exceptions import TwilioRestException

import db
from app import SendError, create_app, send_sms, twilio_sender

NOW = datetime(2026, 10, 5, 9, 0)
PHONE = "+254700000999"


@pytest.fixture
def conn():
    c = db.connect()
    db.add_case(c, id="R-0142", patient_phone=PHONE, service="hiv",
                facility_id="FAC-A", referred_at=NOW.isoformat())
    return c


def ok_sender(to, body):
    return "SM-ok"


def failing_sender(code):
    def send(to, body):
        raise SendError(code)
    return send


def kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


# (id, sender, expected_return, expected_status, expected_event, unreachable_code)
CASES = [
    ("accepted send advances", ok_sender, True, "contacted", "sent", None),
    ("unverified number (21608)", failing_sender(21608), False, "referred", "send_failed", 21608),
    ("opted-out number (21610)", failing_sender(21610), False, "referred", "send_failed", 21610),
    ("network failure", failing_sender("network"), False, "referred", "send_failed", "network"),
]


@pytest.mark.parametrize("cid,sender,ret,status,event,code", CASES, ids=[c[0] for c in CASES])
def test_send_sms_only_advances_on_success(conn, cid, sender, ret, status, event, code):
    assert send_sms(conn, sender, "R-0142", PHONE, "hello", NOW, advance_to="contacted") is ret
    assert db.get_case(conn, "R-0142")["status"] == status
    assert kinds(conn) == [event]
    unreachable = db.unreachable(conn)
    assert [u["error_code"] for u in unreachable] == ([code] if code else [])


def test_later_success_clears_unreachable(conn):
    send_sms(conn, failing_sender(21608), "R-0142", PHONE, "hello", NOW, advance_to="contacted")
    send_sms(conn, ok_sender, "R-0142", PHONE, "hello", NOW, advance_to="contacted")
    assert db.unreachable(conn) == []
    assert db.get_case(conn, "R-0142")["status"] == "contacted"


def test_failed_send_to_clinic_does_not_mark_patient_unreachable(conn):
    send_sms(conn, failing_sender(21608), "R-0142", "+254700000101", "R-0142 arriving", NOW)
    assert db.unreachable(conn) == []


class FakeMessages:
    def __init__(self, exc):
        self.exc = exc

    def create(self, **kw):
        raise self.exc


class FakeClient:
    def __init__(self, exc):
        self.messages = FakeMessages(exc)


@pytest.mark.parametrize("exc,code", [
    (TwilioRestException(400, "/Messages", "unverified", code=21608), 21608),
    (TwilioRestException(500, "/Messages", "server error"), 500),
    (ConnectionError("tunnel down"), "network"),
])
def test_twilio_adapter_maps_errors(exc, code):
    send = twilio_sender(FakeClient(exc), "+15550000000")
    with pytest.raises(SendError) as e:
        send(PHONE, "hello")
    assert e.value.code == code


def test_api_state_lists_unreachable(conn):
    send_sms(conn, failing_sender(21608), "R-0142", PHONE, "hello", NOW)
    client = create_app(auth_token="t", conn=conn).test_client()
    body = client.get("/api/state").get_json()
    assert body["unreachable"] == [{
        "case_id": "R-0142", "phone": PHONE, "status": "referred",
        "failed_at": NOW.isoformat(), "error_code": 21608,
    }]
