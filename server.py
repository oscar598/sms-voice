#!/usr/bin/env python3
"""
Missed-appointment follow-up chatbot over SMS (hackathon proof of concept).

Laptop (server, calls Claude)  --hotspot-->  Android running "SMS Gateway for Android"
                                               ~~~~ SMS ~~~~>  patient's basic phone

Flow:
  1. `send` texts every patient whose appointment has passed and attended == False,
     asking why they missed it.
  2. Each reply arrives at /sms. Claude (classify.chat_json) reads the conversation,
     classifies the reason, and drafts a short reply.
  3. Guardrails wrap the model:
       - STOP and emergency words are handled by fixed rules, never by Claude
       - the model must return structured JSON; anything malformed or low-confidence
         is handed to a staff member instead of guessed
       - the bot never books, diagnoses, or gives medical advice; it collects
         information and flags staff to act (human in the loop)
       - conversations close after MAX_TURNS and hand off to staff

Commands:
    python server.py serve                      # webhook + dashboard on :5000
    python server.py register https://X/sms     # tell the phone where to send replies
    python server.py send                       # message all patients who missed
    python server.py send --id P99999           # message one patient (any date)
    python server.py chat --id P99999           # test the bot in the terminal, no SMS
    python server.py directions --id P99999     # print the transport directions SMS, no SMS
    python server.py reset                      # clear conversations between demo runs

Config (environment variables, or .env in this folder):
    GATEWAY_URL   e.g. http://192.168.43.1:8080   (shown in the app)
    GATEWAY_USER / GATEWAY_PASS                   (shown in the app)
    ANTHROPIC_API_KEY                             Claude API key
    CLASSIFY_MODEL default claude-opus-5-5        (shared with classify.py)
    LLM_TIMEOUT   default 30 seconds per Claude call
    GOOGLE_MAPS_API_KEY                           Routes API key (transport directions)
    CLINIC_NAME / CLINIC_ADDRESS                  destination for directions; a
                                                  `clinic_address` CSV column overrides it
    CLINIC_PHONE                                  number in the transportation-assistance line
    PATIENTS_CSV  default patients.csv
    CONVOS_JSON   default conversations.json
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import threading
from datetime import datetime

import requests
from flask import Flask, jsonify, request

import envfile

if __name__ == "__main__":
    # Same .env loader as app.py / eval.py, and only when run as a script, so
    # importing this module (e.g. from tests) never pulls in a real API key.
    # Must run before the settings below are read.
    envfile.load()

import classify  # noqa: E402
import core  # noqa: E402
import directions  # noqa: E402
import templates  # noqa: E402

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://192.168.43.1:8080").rstrip("/")
GATEWAY_USER = os.environ.get("GATEWAY_USER", "")
GATEWAY_PASS = os.environ.get("GATEWAY_PASS", "")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30"))
CLINIC_NAME = os.environ.get("CLINIC_NAME", "The clinic")
CLINIC_ADDRESS = os.environ.get("CLINIC_ADDRESS", "")
CLINIC_PHONE = os.environ.get("CLINIC_PHONE", "")
PATIENTS_CSV = os.environ.get("PATIENTS_CSV", "patients.csv")
CONVOS_JSON = os.environ.get("CONVOS_JSON", "conversations.json")

MAX_TURNS = 4            # patient messages before handing off to staff
MAX_REPLY_CHARS = 300    # about two SMS segments
MIN_CONFIDENCE = 0.6

EXTRA_COLUMNS = ["miss_reason", "wants_reschedule", "needs_followup", "opted_out", "last_reply"]
REASONS = ["transport", "cost", "illness", "forgot", "work_or_childcare",
           "felt_better", "clinic_issue", "other", "unclear"]
# STOP words and emergency words are shared with the referral agent: core.STOP_WORDS
# and core.EMERGENCY_TERMS (extend those for the local language).
EMERGENCY_REPLY = ("This sounds urgent. In an urgent emergency, call 911 now or go to the "
                   "nearest health facility. A clinic staff member has been alerted.")

SYSTEM_PROMPT = f"""You are a polite follow-up assistant for a small rural health clinic.
You talk with patients by SMS after they missed an appointment.

Your goals, in order:
1. Understand WHY they missed it.
2. Ask whether they want to come back. If yes, ask what day or time suits them.
3. Close politely once you know the reason and whether they want to return.

Rules you must follow:
- Reply in the SAME LANGUAGE the patient writes in.
- Keep replies under 250 characters, plain text, no emojis, warm and simple.
- NEVER give medical advice, diagnose, or comment on symptoms or medication.
  If they ask a medical question, say a health worker will call them.
- NEVER confirm or book a specific appointment time. Say clinic staff will confirm it.
- NEVER mention the reason for their visit or any health condition.
- Do not invent facts about the clinic (hours, prices, services, transport).
- If transport is the problem, do not give directions or a phone number yourself: the
  clinic's system texts them the clinic address, routes and a help number separately.
- If you are unsure what they mean, ask one short clarifying question.

Fill in every field of the JSON response:
- reply: the SMS text to send
- reason: one of {REASONS}
- wants_reschedule: true, false, or null if unknown
- preferred_time: what they said about timing, or an empty string
- needs_staff: true if a human should contact them (medical question, cost help,
  complaint, anything you cannot handle), else false
- done: true if the conversation can close, else false
- confidence: number from 0 to 1, how sure you are you understood them"""

# Claude's structured output is constrained to this shape (classify.chat_json).
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "reason": {"type": "string", "enum": REASONS},
        "wants_reschedule": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "preferred_time": {"type": "string"},
        "needs_staff": {"type": "boolean"},
        "done": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["reply", "reason", "wants_reschedule", "preferred_time",
                 "needs_staff", "done", "confidence"],
    "additionalProperties": False,
}

csv_lock = threading.Lock()
event_log: list[str] = []

app = Flask(__name__)


# ---------------- helpers ----------------

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    event_log.append(line)
    del event_log[:-300]


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def key_for(phone: str) -> str:
    return digits(phone)[-10:]


def is_real_number(phone: str) -> bool:
    d = digits(phone)
    return len(d) >= 10 and not d.startswith("000")


def load_patients() -> tuple[list[dict], list[str]]:
    with open(PATIENTS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for col in EXTRA_COLUMNS:
        if col not in fields:
            fields.append(col)
            for r in rows:
                r[col] = "" if col in ("miss_reason", "wants_reschedule", "last_reply") else "False"
    return rows, fields


def save_patients(rows: list[dict], fields: list[str]) -> None:
    tmp = PATIENTS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, PATIENTS_CSV)


def load_convos() -> dict:
    if not os.path.exists(CONVOS_JSON):
        return {}
    with open(CONVOS_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_convos(convos: dict) -> None:
    tmp = CONVOS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(convos, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONVOS_JSON)


def clean_number(phone: str) -> str:
    """Normalize to +<digits> so the gateway never sees spaces, dashes,
    quotes or invisible characters copied in from spreadsheets."""
    d = digits(phone).lstrip("0")   # country codes never start with 0 ("00" is a dialing prefix)
    if len(d) == 10:                # bare US number like 7865551234
        d = "1" + d
    return "+" + d


def gateway_send(phone: str, text: str, dry_run: bool = False) -> bool:
    phone = clean_number(phone)
    if dry_run:
        return True
    try:
        r = requests.post(
            f"{GATEWAY_URL}/message",
            auth=(GATEWAY_USER, GATEWAY_PASS),
            json={"textMessage": {"text": text}, "phoneNumbers": [phone]},
            timeout=10,
        )
        if r.ok:
            log(f"SENT to {phone}: {text!r}")
            return True
        log(f"GATEWAY ERROR {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"GATEWAY UNREACHABLE ({GATEWAY_URL}): {e}")
    return False


def opening_message(p: dict) -> str:
    # Says nothing about WHY the visit was booked: phones are often shared.
    first = p["name"].split()[0]
    when = datetime.strptime(p["appointment_datetime"], "%Y-%m-%d %H:%M")
    return (f"Hello {first}, this is the clinic. We missed you at your appointment on "
            f"{when.strftime('%a %d %b')}. Is everything okay? Could you tell us why you "
            f"could not come? Reply STOP to stop messages.")


# ---------------- the AI step ----------------

def ask_claude(history: list[dict]) -> dict | None:
    """Return Claude's parsed JSON decision, or None if anything goes wrong."""
    try:
        out = classify.chat_json(SYSTEM_PROMPT, history, REPLY_SCHEMA, timeout=LLM_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - any failure falls back to a human
        log(f"  Claude error: {e}")
        return None

    # Validate: malformed output is treated as "not sure", never sent as-is.
    reply = str(out.get("reply", "")).strip()
    if not reply:
        return None
    try:
        conf = float(out.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "reply": reply[:MAX_REPLY_CHARS],
        "reason": out.get("reason") if out.get("reason") in REASONS else "unclear",
        "wants_reschedule": out.get("wants_reschedule") if isinstance(out.get("wants_reschedule"), bool) else None,
        "preferred_time": str(out.get("preferred_time") or "")[:100],
        "needs_staff": bool(out.get("needs_staff")),
        "done": bool(out.get("done")),
        "confidence": conf,
    }


HANDOFF_REPLY = "Thank you for your message. A clinic staff member will contact you soon."


def transport_directions(patient: dict) -> str | None:
    """Clinic address, car / public transport / bicycle routes, and the assistance line."""
    help_line = templates.TRANSPORT_HELP.format(phone=CLINIC_PHONE) if CLINIC_PHONE else ""
    if not help_line:
        log("  transport help line skipped: CLINIC_PHONE is not set")
    clinic_address = (patient.get("clinic_address") or CLINIC_ADDRESS).strip()
    if not clinic_address:
        log("  directions skipped: CLINIC_ADDRESS is not set")
        return help_line or None
    return directions.transport_sms(patient.get("address", "").strip(), CLINIC_NAME,
                                    clinic_address, log=log, footer=help_line)


def handle_message(phone: str, text: str, dry_run: bool = False) -> str | None:
    """Process one incoming patient message; returns the reply sent (or None)."""
    with csv_lock:
        rows, fields = load_patients()
        convos = load_convos()
        k = key_for(phone)
        patient = next((r for r in rows if key_for(r["phone"]) == k), None)
        if patient is None:
            log(f"  unknown number {phone}; ignored")
            return None

        convo = convos.setdefault(k, {"patient_id": patient["patient_id"],
                                      "status": "open", "messages": []})
        extra = None  # a second SMS after the reply (transport directions)
        convo["messages"].append({"role": "user", "content": text,
                                  "time": datetime.now().isoformat(timespec="seconds")})
        patient["last_reply"] = text
        pid = patient["patient_id"]

        # --- Rule 1: opt-out, never routed to the model ---
        if core.is_stop(text):
            patient["opted_out"] = "True"
            convo["status"] = "closed"
            reply = "You will not receive more messages from the clinic."
            log(f"  {pid} opted out")

        # --- Rule 2: emergencies get a fixed message, never a Claude one ---
        elif core.emergency_hit(text):
            patient["needs_followup"] = "True"
            convo["status"] = "closed"
            reply = EMERGENCY_REPLY
            log(f"  !!! {pid} possible EMERGENCY -> fixed referral, staff alerted")

        # --- Rule 3: conversation too long -> hand to a person ---
        elif convo["status"] == "closed" or \
                sum(m["role"] == "user" for m in convo["messages"]) > MAX_TURNS:
            patient["needs_followup"] = "True"
            convo["status"] = "closed"
            reply = HANDOFF_REPLY
            log(f"  {pid} conversation over turn limit / closed -> staff")

        # --- The AI step ---
        else:
            log(f"  asking Claude ({classify.model()})...")
            d = ask_claude(convo["messages"])
            if d is None or d["confidence"] < MIN_CONFIDENCE:
                patient["needs_followup"] = "True"
                convo["status"] = "closed"
                reply = HANDOFF_REPLY
                why = "Claude failed" if d is None else f"low confidence {d['confidence']:.2f}"
                log(f"  {pid} {why} -> not guessing, staff will follow up")
            else:
                reply = d["reply"]
                if d["reason"] != "unclear":
                    patient["miss_reason"] = d["reason"]
                if d["wants_reschedule"] is not None:
                    patient["wants_reschedule"] = str(d["wants_reschedule"])
                if d["needs_staff"] or d["wants_reschedule"]:
                    patient["needs_followup"] = "True"
                if d["done"]:
                    convo["status"] = "closed"
                if d["reason"] == "transport" and not convo.get("directions_sent"):
                    # Facts come from Google Maps via code, never from Claude; once per conversation.
                    extra = transport_directions(patient)
                    convo["directions_sent"] = bool(extra)
                log(f"  {pid} reason={d['reason']} reschedule={d['wants_reschedule']} "
                    f"time={d['preferred_time']!r} staff={d['needs_staff']} "
                    f"done={d['done']} conf={d['confidence']:.2f}")

        replies = [reply] + ([extra] if extra else [])
        for r in replies:
            convo["messages"].append({"role": "assistant", "content": r,
                                      "time": datetime.now().isoformat(timespec="seconds")})
        save_patients(rows, fields)
        save_convos(convos)

    for r in replies:
        gateway_send(patient["phone"], r, dry_run=dry_run)
    return "\n".join(replies)


# ---------------- web endpoints ----------------

@app.post("/sms")
def receive_sms():
    data = request.get_json(silent=True) or {}
    if data.get("event") != "sms:received":
        return jsonify(ok=True, ignored=True)
    p = data.get("payload", {})
    sender, text = p.get("phoneNumber", ""), (p.get("message") or "").strip()
    log(f"RECEIVED from {sender}: {text!r}")
    if text:
        # Reply in the background so the gateway's webhook call returns quickly.
        threading.Thread(target=handle_message, args=(sender, text), daemon=True).start()
    return jsonify(ok=True)


@app.get("/")
def dashboard():
    with csv_lock:
        rows, fields = load_patients()
        convos = load_convos()
    shown = [r for r in rows if is_real_number(r["phone"])] or rows[:10]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in fields)

    def cell(c, v):
        v = str(v)
        style = ""
        if c == "needs_followup" and v == "True":
            style = ' style="background:#fff3cd;font-weight:600"'
        elif c == "miss_reason" and v:
            style = ' style="font-weight:600"'
        return f"<td{style}>{html.escape(v)}</td>"

    body = "".join("<tr>" + "".join(cell(c, r.get(c, "")) for c in fields) + "</tr>" for r in shown)

    chats = ""
    for c in convos.values():
        bubbles = "".join(
            f'<div class="b {m["role"]}">{html.escape(m["content"])}</div>' for m in c["messages"])
        chats += (f'<div class="chat"><b>{html.escape(c["patient_id"])}</b> '
                  f'<span class="st">{c["status"]}</span>{bubbles}</div>')
    logs = html.escape("\n".join(reversed(event_log[-40:])))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3"><title>Clinic Follow-up</title><style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;font-size:13px}}td,th{{border:1px solid #ddd;padding:4px 8px}}
th{{background:#f4f4f4;text-align:left}}
.chats{{display:flex;gap:16px;flex-wrap:wrap}}.chat{{border:1px solid #ddd;border-radius:8px;padding:10px;width:320px}}
.b{{padding:6px 10px;border-radius:12px;margin:6px 0;font-size:14px;max-width:85%}}
.assistant{{background:#e8f0fe}}.user{{background:#e6f4ea;margin-left:auto;text-align:right}}
.st{{font-size:11px;color:#888}}
pre{{background:#111;color:#0f0;padding:12px;font-size:12px;max-height:260px;overflow:auto}}
</style></head><body>
<h2>Clinic Missed-Appointment Follow-up</h2>
<p>Model: Claude ({html.escape(classify.model())}) · refreshes every 3s</p>
<h3>Conversations</h3><div class="chats">{chats or "(none yet)"}</div>
<h3>Patients</h3><table><tr>{head}</tr>{body}</table>
<h3>Log</h3><pre>{logs or "(no events yet)"}</pre></body></html>"""


# ---------------- CLI ----------------

def start_conversation(p: dict, convos: dict, dry_run: bool = False) -> None:
    msg = opening_message(p)
    convos[key_for(p["phone"])] = {
        "patient_id": p["patient_id"], "status": "open",
        "messages": [{"role": "assistant", "content": msg,
                      "time": datetime.now().isoformat(timespec="seconds")}],
    }
    gateway_send(p["phone"], msg, dry_run=dry_run)


def cmd_send(only_id: str | None) -> None:
    rows, _ = load_patients()
    convos = load_convos()
    now = datetime.now()
    sent = 0
    for p in rows:
        if only_id:
            if p["patient_id"] != only_id:
                continue
        else:
            missed = (p["attended"] != "True" and
                      datetime.strptime(p["appointment_datetime"], "%Y-%m-%d %H:%M") < now)
            if not missed:
                continue
        if p.get("opted_out") == "True" or not is_real_number(p["phone"]):
            continue
        start_conversation(p, convos)
        sent += 1
    save_convos(convos)
    print(f"Started {sent} conversation(s).")
    if sent == 0:
        print("Tip: only patients with a real number and a PAST appointment are messaged. "
              "Use --id to message a specific patient.")


def find_patient(pid: str) -> dict:
    rows, _ = load_patients()
    p = next((r for r in rows if r["patient_id"] == pid), None)
    if p is None:
        sys.exit(f"No patient {pid}")
    return p


def cmd_chat(pid: str) -> None:
    """Try the bot in the terminal without sending any SMS."""
    p = find_patient(pid)
    convos = load_convos()
    start_conversation(p, convos, dry_run=True)
    save_convos(convos)
    print(f"BOT: {convos[key_for(p['phone'])]['messages'][0]['content']}")
    while True:
        try:
            text = input("YOU: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        reply = handle_message(p["phone"], text, dry_run=True)
        print(f"BOT: {reply}")
        if load_convos()[key_for(p["phone"])]["status"] == "closed":
            print("(conversation closed)")
            break


def cmd_directions(pid: str) -> None:
    """Print the transport directions SMS for one patient, without sending it."""
    p = find_patient(pid)
    print(f"From: {p.get('address') or '(no address)'}")
    print(transport_directions(p) or "(no directions: set CLINIC_ADDRESS)")


def cmd_reset() -> None:
    """Clear all conversations and bot-filled columns (keeps patients and attended)."""
    with csv_lock:
        rows, fields = load_patients()
        for r in rows:
            r["miss_reason"] = r["wants_reschedule"] = r["last_reply"] = ""
            r["needs_followup"] = r["opted_out"] = "False"
        save_patients(rows, fields)
        save_convos({})
    print(f"Reset {len(rows)} patients and cleared {CONVOS_JSON}.")


def cmd_register(url: str) -> None:
    r = requests.post(f"{GATEWAY_URL}/webhooks", auth=(GATEWAY_USER, GATEWAY_PASS),
                      json={"id": "clinic-replies", "url": url, "event": "sms:received"},
                      timeout=10)
    print(r.status_code, r.text)


def main() -> None:
    ap = argparse.ArgumentParser(description="Missed-appointment SMS chatbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=5000)
    sd = sub.add_parser("send"); sd.add_argument("--id")
    ch = sub.add_parser("chat"); ch.add_argument("--id", required=True)
    dr = sub.add_parser("directions"); dr.add_argument("--id", required=True)
    rg = sub.add_parser("register"); rg.add_argument("url")
    sub.add_parser("reset")
    args = ap.parse_args()

    if not os.path.exists(PATIENTS_CSV):
        sys.exit(f"{PATIENTS_CSV} not found. Run generate_patients.py first.")
    if args.cmd == "serve":
        log(f"Server starting; dashboard at http://localhost:{args.port}/  model=Claude ({classify.model()})")
        app.run(host="0.0.0.0", port=args.port, threaded=True)
    elif args.cmd == "send":
        cmd_send(args.id)
    elif args.cmd == "chat":
        cmd_chat(args.id)
    elif args.cmd == "directions":
        cmd_directions(args.id)
    elif args.cmd == "register":
        cmd_register(args.url)
    elif args.cmd == "reset":
        cmd_reset()


if __name__ == "__main__":
    main()