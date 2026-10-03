"""Referral closure agent: HTTP entry points.

T1 scope: the Twilio webhook (/sms) rejects any request that does not carry a
valid X-Twilio-Signature (design doc, ledger R1 / D2). Routing, classification
and rules land in core.py (T2); /sms only acknowledges for now.
"""

import hmac
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

import classify
import core
import db
import metrics
import scheduler
import seed
import templates

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

    STOP is enforced here, the one path every SMS takes (R5d): nothing goes
    to the phone of an opted-out patient, whichever feature asked.
    """
    ts = now.isoformat()
    case = db.get_case(conn, case_id)
    if case and case["opted_out"] and to == case["patient_phone"]:
        db.log_event(conn, case_id, ts, "agent", "suppressed_opted_out", to=to)
        conn.commit()
        return False
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


QUOTE_MAX = 60


def _quote(text):
    text = " ".join((text or "").split())
    return text if len(text) <= QUOTE_MAX else text[: QUOTE_MAX - 3] + "..."


def start_referral(conn, send, case, now, data=seed):
    """Create a case and send the first outreach SMS."""
    db.add_case(conn, referred_at=now.isoformat(), **case)
    fac = data.FACILITY_BY_ID[case["facility_id"]]
    body = templates.render(templates.PATIENT, "intro", name=fac["name"], service=case["service"])
    db.update_case(conn, case["id"], awaiting="free_text")
    send_sms(conn, send, case["id"], case["patient_phone"], body, now, advance_to="contacted")


def _escalate(conn, send, case, kind, quote, now, data):
    db.update_case(conn, case["id"], escalation=kind, awaiting=None)
    db.log_event(conn, case["id"], now.isoformat(), "agent", "escalated", level=kind)
    # Baton timeouts / supervisor fallback are not built (R6: stuck stays stuck).
    chw = data.CHWS[0]
    body = templates.render(templates.CHW, "baton", kind=kind.upper().replace("_", "-"),
                            case_id=case["id"], phone=case["patient_phone"], quote=_quote(quote))
    send_sms(conn, send, case["id"], chw["phone"], body, now)


def _execute(conn, send, case, cls, plan, body, now, data):
    """Apply a core.Plan: log, update the case, send patient/clinic/CHW SMS."""
    ts = now.isoformat()
    cid = case["id"]
    if cls.label not in ("plan_ack", "unknown"):
        # Barrier events carry the facility at the time; the radar reads these.
        db.log_event(conn, cid, ts, "agent", "barrier", barrier=cls.label,
                     facility_id=case["facility_id"], reason=cls.reason)
        db.update_case(conn, cid, latest_barrier=cls.label, status="barrier_found")
    if plan.repoint_to:
        db.update_case(conn, cid, facility_id=plan.repoint_to)
    if plan.set_visit_date:
        db.update_case(conn, cid, visit_date=plan.set_visit_date, status="visit_scheduled")

    key, slots = plan.patient
    sms = templates.render(templates.PATIENT, key, **slots)
    advance = None if plan.escalate or plan.set_visit_date or cls.label == "plan_ack" else "action_taken"
    send_sms(conn, send, cid, case["patient_phone"], sms, now, advance_to=advance)

    if plan.clinic:
        fac_id, ckey, cslots = plan.clinic
        send_sms(conn, send, cid, data.FACILITY_BY_ID[fac_id]["phone"],
                 templates.render(templates.CLINIC, ckey, **cslots), now)
    if plan.escalate:
        _escalate(conn, send, case, plan.escalate, body, now, data)
    else:
        db.update_case(conn, cid, awaiting="free_text")
    conn.commit()


def _pick(conn, sql, args, explicit_id):
    """Resolve the case a staff reply refers to: explicit id, or the only candidate."""
    if explicit_id:
        return db.get_case(conn, explicit_id)
    rows = conn.execute(sql, args).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def handle_inbound(conn, send, from_phone, body, now, classify_fn=None, data=seed):
    """Single entry for /sms and /sim: route, decide, act. Returns the route kind.

    Reads a case, then writes it, with no guard against a concurrent change.
    """
    # gstack-shortcut(dec-6e0a360e): correct only with one worker thread (no guarded
    # UPDATEs on case transitions), upgrade when running >1 worker, a separate
    # scheduler process, or any background thread that touches cases.
    classify_fn = classify_fn or classify.classify
    ts = now.isoformat()
    today = now.date()

    chw = next((c for c in data.CHWS if c["phone"] == from_phone), None)
    clinic = next((f for f in data.FACILITIES if f["phone"] == from_phone), None)

    if chw or clinic:
        role = "chw" if chw else "clinic"
        r = core.route(body, role)
        if role == "chw":
            case = _pick(conn, "SELECT * FROM cases WHERE escalation != 'none' AND claimed_by_chw IS NULL",
                         (), r.case_id)
        else:
            case = _pick(conn, "SELECT * FROM cases WHERE facility_id = ?"
                         " AND status IN ('visit_scheduled', 'follow_up')",
                         (clinic["id"],), r.case_id)
        if r.kind == "unparsed" or case is None:
            reply = templates.CHW["which"] if role == "chw" else templates.STAFF_WHICH
            send(from_phone, reply)
            return r.kind if r.kind == "unparsed" else "which"
        cid = case["id"]
        db.log_event(conn, cid, ts, role, "inbound", body=body, route=r.kind)
        if r.kind == "chw_claim":
            if case["escalation"] == "none":
                send_sms(conn, send, cid, from_phone,
                         templates.render(templates.CHW, "not_open", case_id=cid), now)
            else:
                db.update_case(conn, cid, claimed_by_chw=chw["id"])
                send_sms(conn, send, cid, from_phone,
                         templates.render(templates.CHW, "claimed", case_id=cid), now)
        elif r.kind == "chw_done":
            status = "visit_scheduled" if case["visit_date"] else "action_taken"
            db.update_case(conn, cid, escalation="none", claimed_by_chw=None,
                           status=status, awaiting="free_text")
        elif r.kind == "chw_lost":
            db.update_case(conn, cid, status="lost", escalation="none", awaiting=None)
        elif r.kind == "clinic_seen":
            db.complete_case(conn, cid, "clinic", ts)
        elif r.kind == "clinic_not_seen":
            db.log_event(conn, cid, ts, "clinic", "clinic_not_seen")
            if db.last_event(conn, cid, ["patient_yes"]):  # patient YES + clinic N: conflict
                _escalate(conn, send, case, "non_clinical", "[patient said YES, clinic said N]", now, data)
        conn.commit()
        return r.kind

    case = db.open_case_for_phone(conn, from_phone)
    if case is None:
        return "no_case"
    cid = case["id"]
    r = core.route(body, "patient", case, case["awaiting"])
    db.log_event(conn, cid, ts, "patient", "inbound", body=body, route=r.kind)
    db.update_case(conn, cid, reminder_count=0)  # any reply resets the reminder count
    facility = data.FACILITY_BY_ID[case["facility_id"]]

    if r.kind == "clinical":
        cls = core.Classification("clinical_symptom", urgent=True, reason="keyword")
        plan = core.decide(cls, case, facility, data.FACILITIES, today, data.EMERGENCY)
        _execute(conn, send, case, cls, plan, body, now, data)
    elif r.kind == "opt_out":
        db.update_case(conn, cid, opted_out=1, awaiting=None)
    elif r.kind == "opt_in":
        db.update_case(conn, cid, opted_out=0, awaiting="free_text")
    elif r.kind == "holding":
        send_sms(conn, send, cid, case["patient_phone"], templates.PATIENT["holding"], now)
    elif r.kind == "yes":
        # Seen, per the patient. Clinic Y completes now; clinic silence for 48 h
        # completes as patient-reported (scheduler); clinic N is a conflict.
        db.update_case(conn, cid, awaiting=None)
        db.log_event(conn, cid, ts, "patient", "patient_yes")
        if db.last_event(conn, cid, ["clinic_not_seen"]):
            _escalate(conn, send, case, "non_clinical", "[patient said YES, clinic said N]", now, data)
    elif r.kind == "no":
        db.update_case(conn, cid, status="barrier_found", awaiting="free_text")
        db.log_event(conn, cid, ts, "patient", "patient_no")
        send_sms(conn, send, cid, case["patient_phone"], templates.PATIENT["what_happened"], now)
    elif r.kind == "classify":
        try:
            raw, error = classify_fn(body, today), False
        except Exception as e:  # any model/API failure is handled by the guards (D6)
            raw, error = None, True
            db.log_event(conn, cid, ts, "agent", "model_error", error=type(e).__name__)
        cls = core.apply_guards(raw, today, data.AREAS, facility, error=error)
        plan = core.decide(cls, case, facility, data.FACILITIES, today, data.EMERGENCY)
        _execute(conn, send, case, cls, plan, body, now, data)
    conn.commit()
    return r.kind


OPEN_CASE_MSG = "This phone already has an open referral."
KENYAN_MOBILE = re.compile(r"^\+254[17]\d{8}$")


def next_case_id(conn):
    row = conn.execute("SELECT MAX(CAST(SUBSTR(id, 3) AS INTEGER)) FROM cases WHERE id LIKE 'R-%'").fetchone()
    return core.case_id((row[0] or 0) + 1)


def validate_referral(payload, conn, data=seed):
    """Check a new-referral request. Returns (case fields, errors by field)."""
    payload = payload if isinstance(payload, dict) else {}
    errors = {}
    phone = re.sub(r"[\s-]", "", str(payload.get("patient_phone", "")))
    if phone.startswith("07") or phone.startswith("01"):
        phone = "+254" + phone[1:]
    if not KENYAN_MOBILE.match(phone):
        errors["patient_phone"] = "Use a Kenyan mobile number, e.g. +254712345678 or 0712345678."
    elif db.open_case_for_phone(conn, phone):
        errors["patient_phone"] = OPEN_CASE_MSG
    fac = data.FACILITY_BY_ID.get(payload.get("facility_id"))
    if not fac:
        errors["facility_id"] = "Pick one of the listed facilities."
    service = payload.get("service")
    if fac and service not in fac["services"]:
        errors["service"] = f"{fac['name']} offers: {', '.join(fac['services'])}."
    area = payload.get("area") or (fac["area"] if fac else None)
    if area not in data.AREAS:
        errors["area"] = f"Pick one of: {', '.join(data.AREAS)}."
    case = {"patient_phone": phone, "service": service, "facility_id": payload.get("facility_id"),
            "patient_area": area}
    return case, errors


def sim_sender(transcript):
    """SIM_MODE sender: outbound goes to the on-screen transcript, never Twilio."""
    sent = []

    def send(to, body):
        transcript.append({"dir": "out", "phone": to, "body": body})
        sent.append(to)
        return f"SIM-{len(sent)}"
    return send


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


def _twilio_from_env():
    from twilio.rest import Client

    sid, number = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_FROM")
    if not (sid and number):
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_FROM are required unless SIM_MODE=1")
    return twilio_sender(Client(sid, os.environ["TWILIO_AUTH_TOKEN"]), number)


def create_app(auth_token=None, conn=None, sender=None, sim_mode=None,
               classify_fn=None, now_fn=None):
    token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        # Without the token every signature check fails; refuse to start
        # rather than run a webhook that rejects (or worse, skips) everything.
        raise RuntimeError("TWILIO_AUTH_TOKEN is not set")
    if sim_mode is None:
        sim_mode = os.environ.get("SIM_MODE") == "1"

    app = Flask(__name__)
    validator = RequestValidator(token)
    conn = conn or db.connect(os.environ.get("DB_PATH", "referrals.db"))
    base_now = now_fn or datetime.now
    # gstack-shortcut(dec-b15e8613): fast-forward moves ONE clock for every case, so
    # all seeded timers fire together and may send stray SMS on stage; upgrade
    # (per-case fast-forward) when a rehearsal shows stray SMS or dashboard jumps.
    clock = {"offset": timedelta(0)}

    def now_fn():
        return base_now() + clock["offset"]

    transcript = []
    if sender is None:
        # SIM_MODE never talks to Twilio: outbound lands in the on-screen transcript.
        sender = sim_sender(transcript) if sim_mode else _twilio_from_env()

    @app.before_request
    def run_due_timers():
        # Same thread as the request (D3): polling from /sim and the dashboard
        # drives the timers. A timer failure must never break an inbound SMS.
        try:
            scheduler.tick(conn, sender, now_fn(), seed)
        except Exception:
            app.logger.exception("scheduler tick failed")

    def signature_ok():
        signature = request.headers.get("X-Twilio-Signature", "")
        return bool(signature) and validator.validate(
            public_url(request), request.form.to_dict(), signature
        )

    @app.post("/sms")
    def sms():
        if not signature_ok():
            abort(403)
        handle_inbound(conn, sender, request.form.get("From", ""), request.form.get("Body", ""),
                       now_fn(), classify_fn)
        return Response(EMPTY_TWIML, mimetype="text/xml")

    @app.get("/api/state")
    def state():
        return jsonify({"unreachable": db.unreachable(conn)})

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify(metrics.dashboard(conn, now_fn(), seed.FACILITIES))

    # Comma-separated list of sites allowed to call /api/* from a browser (the Lovable
    # preview and published URLs). "*" or unset = any site. This is browser-side only:
    # it does not stop a direct curl, so it is not authentication.
    # An entry may contain "*", matching letters, digits and dashes only (Lovable preview
    # URLs change per version: https://id-preview*--<project-id>.lovable.app).
    allowed_origins = {o.strip().rstrip("/") for o in os.environ.get("DASHBOARD_ORIGIN", "*").split(",") if o.strip()}
    origin_patterns = [re.compile("^" + re.escape(o).replace(r"\*", "[a-z0-9-]*") + "$")
                       for o in allowed_origins if o != "*"]

    referral_token = os.environ.get("REFERRAL_TOKEN", "")

    @app.get("/api/facilities")
    def facilities():
        return jsonify([{k: f[k] for k in ("id", "name", "area", "services", "hours_text")}
                        for f in seed.FACILITIES])

    @app.post("/api/referrals")
    def create_referral():
        # Creates a case and texts a patient, so unlike the read-only /api/* routes
        # it needs the shared secret. Unset REFERRAL_TOKEN = endpoint off.
        if not referral_token:
            return jsonify({"error": "REFERRAL_TOKEN is not set on the server"}), 503
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), referral_token.encode()):
            return jsonify({"error": "missing or wrong referral token"}), 401
        fields, errors = validate_referral(request.get_json(silent=True), conn)
        if errors:
            return jsonify({"errors": errors}), 409 if errors.get("patient_phone") == OPEN_CASE_MSG else 400
        fields["id"] = next_case_id(conn)
        start_referral(conn, sender, fields, now_fn())
        case = db.get_case(conn, fields["id"])
        return jsonify({"case": case, "intro_sent": case["status"] == "contacted"}), 201

    @app.get("/referrals/new")
    def referral_form():
        return Response((Path(__file__).parent / "referral.html").read_text(), mimetype="text/html")

    @app.after_request
    def cors_for_api(resp):
        # The hosted dashboard (Lovable) calls /api/* from another origin. Reads are
        # open; /api/referrals also accepts POST and needs the Authorization header.
        # Set DASHBOARD_ORIGIN to the Lovable URL(s) to lock it down.
        if request.path.startswith("/api/"):
            writes = request.path == "/api/referrals"
            origin = request.headers.get("Origin", "")
            if "*" in allowed_origins:
                resp.headers["Access-Control-Allow-Origin"] = "*"
            elif any(p.match(origin) for p in origin_patterns):
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
            else:
                resp.headers["Vary"] = "Origin"
                return resp  # no CORS headers: the browser blocks the other site
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS" if writes else "GET, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, ngrok-skip-browser-warning" + (", Authorization" if writes else ""))
        return resp

    if sim_mode:
        # /sim routes exist only in SIM_MODE; production never exposes an
        # unsigned way into the router (D2, D4).
        @app.get("/sim")
        def sim_page():
            return Response((Path(__file__).parent / "sim.html").read_text(), mimetype="text/html")

        @app.get("/sim/config")
        def sim_config():
            fac = seed.FACILITY_BY_ID[seed.DEMO_CASE["facility_id"]]
            return jsonify({"patient": seed.DEMO_PATIENT_PHONE, "chw": seed.CHWS[0]["phone"],
                            "clinic": fac["phone"], "clinic_name": fac["name"]})

        @app.post("/sim/send")
        def sim_send():
            msg = request.get_json(force=True)
            transcript.append({"dir": "in", "phone": msg["from"], "body": msg["body"]})
            kind = handle_inbound(conn, sender, msg["from"], msg["body"], now_fn(), classify_fn)
            return jsonify({"route": kind})

        @app.post("/sim/referral")
        def sim_referral():
            if db.get_case(conn, seed.DEMO_CASE["id"]) is None:
                start_referral(conn, sender, dict(seed.DEMO_CASE), now_fn())
            return jsonify({"case": db.get_case(conn, seed.DEMO_CASE["id"])})

        @app.get("/sim/transcript")
        def sim_transcript():
            case = db.get_case(conn, seed.DEMO_CASE["id"])
            return jsonify({"messages": transcript, "case": case, "now": now_fn().isoformat()})

        @app.post("/sim/fast-forward")
        def sim_fast_forward():
            days = float(request.get_json(force=True).get("days", 1))
            clock["offset"] += timedelta(days=days)
            applied = scheduler.tick(conn, sender, now_fn(), seed)
            return jsonify({"now": now_fn().isoformat(), "applied": applied})

    return app


if __name__ == "__main__":
    import envfile

    envfile.load()  # ANTHROPIC_API_KEY, REFERRAL_TOKEN etc. from .env, unless already set
    # gstack-shortcut(dec-6e0a360e): one worker, one thread is what keeps case
    # transitions race-free; upgrade (guarded UPDATEs) before threaded=True,
    # gunicorn/uvicorn workers > 1, or a scheduler thread.
    create_app().run(port=int(os.environ.get("PORT", "5000")), threaded=False, load_dotenv=False)
