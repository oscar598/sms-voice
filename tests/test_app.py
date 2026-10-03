import pytest
from twilio.request_validator import RequestValidator

import db
from app import create_app

TOKEN = "test-auth-token"
TUNNEL = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "abc123.ngrok.app"}
FORM = {"From": "+254700000101", "Body": "Y R-0142", "MessageSid": "SM123"}


def sign(url, form):
    return RequestValidator(TOKEN).compute_signature(url, form)


@pytest.fixture
def client():
    return create_app(auth_token=TOKEN, conn=db.connect()).test_client()


# (case, headers, signed_url, signed_form, expected_status)
CASES = [
    ("valid, direct", {}, "http://localhost/sms", FORM, 200),
    ("valid, behind tunnel", TUNNEL, "https://abc123.ngrok.app/sms", FORM, 200),
    ("tunnel headers ignored would fail", {}, "https://abc123.ngrok.app/sms", FORM, 403),
    ("no signature header", {}, None, None, 403),
    ("signed for other body", {}, "http://localhost/sms", {**FORM, "Body": "N R-0142"}, 403),
    ("signed with other token", {}, "other-token", FORM, 403),
]


@pytest.mark.parametrize("case,headers,signed_url,signed_form,expected", CASES, ids=[c[0] for c in CASES])
def test_sms_signature(client, case, headers, signed_url, signed_form, expected):
    headers = dict(headers)
    if signed_url == "other-token":
        headers["X-Twilio-Signature"] = RequestValidator("other-token").compute_signature(
            "http://localhost/sms", FORM
        )
    elif signed_url:
        headers["X-Twilio-Signature"] = sign(signed_url, signed_form)

    resp = client.post("/sms", data=FORM, headers=headers, base_url="http://localhost")

    assert resp.status_code == expected
    if expected == 200:
        assert resp.mimetype == "text/xml"
        assert b"<Response>" in resp.data


def test_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN"):
        create_app()
