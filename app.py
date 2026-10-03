"""Referral closure agent: HTTP entry points.

T1 scope: the Twilio webhook (/sms) rejects any request that does not carry a
valid X-Twilio-Signature (design doc, ledger R1 / D2). Routing, classification
and rules land in core.py (T2); /sms only acknowledges for now.
"""

import os

from flask import Flask, Response, abort, request
from twilio.request_validator import RequestValidator

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


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


def create_app(auth_token=None):
    token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        # Without the token every signature check fails; refuse to start
        # rather than run a webhook that rejects (or worse, skips) everything.
        raise RuntimeError("TWILIO_AUTH_TOKEN is not set")

    app = Flask(__name__)
    validator = RequestValidator(token)

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

    return app


if __name__ == "__main__":
    # One worker, one thread: the scheduler (later) shares this process (D3).
    create_app().run(port=int(os.environ.get("PORT", "5000")), threaded=False)
