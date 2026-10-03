"""SQLite schema and queries. Single process, single thread (D3 shortcut)."""

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    patient_phone TEXT NOT NULL,
    patient_lang TEXT DEFAULT 'en',
    patient_area TEXT,
    service TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    referred_at TEXT NOT NULL,
    visit_date TEXT,
    status TEXT NOT NULL DEFAULT 'referred',
    escalation TEXT NOT NULL DEFAULT 'none',
    latest_barrier TEXT,
    claimed_by_chw TEXT,
    opted_out INTEGER NOT NULL DEFAULT 0,
    completed_by TEXT,
    awaiting TEXT,           -- open prompt: 'free_text' | 'yes_no' | NULL
    reminder_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    barrier TEXT,
    facility_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_case(conn, **case):
    cols = ", ".join(case)
    marks = ", ".join("?" for _ in case)
    conn.execute(f"INSERT INTO cases ({cols}) VALUES ({marks})", tuple(case.values()))
    conn.commit()


def get_case(conn, case_id):
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    return dict(row) if row else None


def set_status(conn, case_id, status):
    conn.execute("UPDATE cases SET status = ? WHERE id = ?", (status, case_id))


UPDATABLE = {
    "status", "visit_date", "escalation", "latest_barrier", "claimed_by_chw",
    "opted_out", "completed_by", "awaiting", "facility_id", "reminder_count",
}


def update_case(conn, case_id, **fields):
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE cases SET {sets} WHERE id = ?", (*fields.values(), case_id))


def open_case_for_phone(conn, phone):
    """Most recent case for this patient phone that is not finished.

    Two open cases on one phone is out of scope for v0 (R7b, user direction).
    """
    row = conn.execute(
        "SELECT * FROM cases WHERE patient_phone = ? AND status NOT IN ('completed', 'lost')"
        " ORDER BY referred_at DESC LIMIT 1",
        (phone,),
    ).fetchone()
    return dict(row) if row else None


def events(conn, case_id=None):
    q = "SELECT case_id, ts, actor, kind, barrier, facility_id, payload_json FROM events"
    args = ()
    if case_id:
        q, args = q + " WHERE case_id = ?", (case_id,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def last_event(conn, case_id, kinds, to=None, actor=None):
    """Most recent event of the given kinds, optionally to a phone / from an actor."""
    q = f"SELECT * FROM events WHERE case_id = ? AND kind IN ({','.join('?' for _ in kinds)})"
    args = [case_id, *kinds]
    if to:
        q += " AND json_extract(payload_json, '$.to') = ?"
        args.append(to)
    if actor:
        q += " AND actor = ?"
        args.append(actor)
    row = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def log_event(conn, case_id, ts, actor, kind, barrier=None, facility_id=None, **payload):
    conn.execute(
        "INSERT INTO events (case_id, ts, actor, kind, barrier, facility_id, payload_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (case_id, ts, actor, kind, barrier, facility_id, json.dumps(payload)),
    )


def unreachable(conn):
    """Cases whose most recent patient send attempt failed (D8 / R7a)."""
    rows = conn.execute(
        """
        SELECT c.id, c.patient_phone, c.status, e.ts, e.payload_json
        FROM cases c
        JOIN events e ON e.id = (
            SELECT MAX(id) FROM events
            WHERE case_id = c.id AND kind IN ('sent', 'send_failed')
              AND json_extract(payload_json, '$.to') = c.patient_phone
        )
        WHERE e.kind = 'send_failed'
        ORDER BY e.ts DESC
        """
    ).fetchall()
    return [
        {"case_id": r["id"], "phone": r["patient_phone"], "status": r["status"],
         "failed_at": r["ts"], "error_code": json.loads(r["payload_json"]).get("code")}
        for r in rows
    ]
