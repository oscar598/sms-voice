"""Referral closure agent: HTTP entry points.

T1 scope: the Twilio webhook (/sms) rejects any request that does not carry a
valid X-Twilio-Signature (design doc, ledger R1 / D2). Routing, classification
and rules land in core.py (T2); /sms only acknowledges for now.
"""

import os

from flask import Flask, Response, abort, jsonify, request
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

import db

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


class SendError(Exception):
    def __init__(self, code):
        super().__init__(f"send failed: {code}")
        self.code = code


def twilio_sender(client, from_number):
    """Adapt a Twilio client to `sender(to, body) -> sid`, raising SendError.

    Trial accounts reject unverified numbers (21608); opted-out numbers
    raise 21610. Network failures surface as 'network'.
    """
    def send(to, body):
        try:
            return client.messages.create(to=to, from_=from_number, body=body).sid
        except TwilioRestException as e:
            raise SendError(e.code or e.status) from e
        except OSError as e:
            raise SendError("network") from e
    return send


def send_sms(conn, sender, case_id, to, body, now, advance_to=None):
    """Send one SMS; only a confirmed send may advance the case (D8 / R7a).

    On failure the case keeps its status and a `send_failed` event records the
    code, so the dashboard lists the patient as unreachable instead of
    'contacted'. Returns True when the message was accepted.
    """
    ts = now.isoformat()
    try:
        sid = sender(to, body)
    except SendError as e:
        db.log_event(conn, case_id, ts, "agent", "send_failed", to=to, code=e.code)
        conn.commit()
        return False
    db.log_event(conn, case_id, ts, "agent", "sent", to=to, sid=sid)
    if advance_to:
        db.set_status(conn, case_id, advance_to)
    conn.commit()
    return True


def public_url(req):
    """Rebuild the URL Twilio actually signed.

    Behind ngrok / Cloudflare tunnels TLS ends at the tunnel, so Flask sees
    http://localhost:5000/sms while Twilio signed https://<tunnel-host>/sms.
    Twilio signs scheme, host, path and query string, so all four must match.
    """
    proto = req.headers.get("X-Forwarded-Proto", req.scheme).split(",")[0].strip()
    host = req.headers.get("X-Forwarded-Host", req.host).split(",")[0].strip()
    query = req.query_string.decode()
    return f"{proto}://{host}{req.path}" + (f"?{query}" if query else "")


def create_app(auth_token=None, conn=None):
    token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        # Without the token every signature check fails; refuse to start
        # rather than run a webhook that rejects (or worse, skips) everything.
        raise RuntimeError("TWILIO_AUTH_TOKEN is not set")

    app = Flask(__name__)
    validator = RequestValidator(token)
    conn = conn or db.connect(os.environ.get("DB_PATH", "referrals.db"))

    def signature_ok():
        signature = request.headers.get("X-Twilio-Signature", "")
        return bool(signature) and validator.validate(
            public_url(request), request.form.to_dict(), signature
        )

    @app.post("/sms")
    def sms():
        if not signature_ok():
            abort(403)
        return Response(EMPTY_TWIML, mimetype="text/xml")

    @app.get("/api/state")
    def state():
        return jsonify({"unreachable": db.unreachable(conn)})

    return app


if __name__ == "__main__":
    # One worker, one thread: the scheduler (later) shares this process (D3).
    create_app().run(port=int(os.environ.get("PORT", "5000")), threaded=False)
