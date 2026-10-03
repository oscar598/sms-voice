"""Dashboard numbers, computed from the cases table and the events log.

Radar definition (user decision, 2026-10-03): for each facility over the last
7 days, N = distinct cases with any activity at that facility, and the signal
= cases among them that reported clinic_closed or turned_away there. Facilities
with N < 5 are hidden. It is a signal that needs follow-up, not a score: it
rests on patient self-report.
"""

import statistics
from datetime import datetime, timedelta

import db

RADAR_WINDOW = timedelta(days=7)
RADAR_MIN_N = 5
RADAR_BARRIERS = ("clinic_closed", "turned_away")


def _radar(conn, since, facilities):
    rows = []
    for f in facilities:
        n = conn.execute(
            """
            SELECT COUNT(DISTINCT case_id) FROM (
                SELECT e.case_id FROM events e JOIN cases c ON c.id = e.case_id
                WHERE c.facility_id = ? AND e.ts >= ?
                UNION
                SELECT case_id FROM events WHERE facility_id = ? AND ts >= ?
            )
            """, (f["id"], since, f["id"], since)).fetchone()[0]
        flagged = conn.execute(
            f"""
            SELECT COUNT(DISTINCT case_id) FROM events
            WHERE kind = 'barrier' AND facility_id = ? AND ts >= ?
              AND barrier IN ({','.join('?' for _ in RADAR_BARRIERS)})
            """, (f["id"], since, *RADAR_BARRIERS)).fetchone()[0]
        rows.append({
            "facility_id": f["id"], "name": f["name"], "area": f["area"],
            "flagged": flagged, "n": n,
            "share": round(flagged / n, 3) if n else 0.0,
            "hidden": n < RADAR_MIN_N,
        })
    return sorted(rows, key=lambda r: (r["hidden"], -r["share"], r["name"]))


def dashboard(conn, now, facilities):
    by_id = {f["id"]: f for f in facilities}
    cases = [dict(r) for r in conn.execute("SELECT * FROM cases ORDER BY referred_at DESC")]
    total = len(cases)
    completed = [c for c in cases if c["status"] == "completed"]

    durations = []
    for c in completed:
        ev = db.last_event(conn, c["id"], ["completed"])
        if ev:
            delta = datetime.fromisoformat(ev["ts"]) - datetime.fromisoformat(c["referred_at"])
            durations.append(delta.total_seconds() / 86400)

    escalated_ids = {r[0] for r in conn.execute("SELECT DISTINCT case_id FROM events WHERE kind = 'escalated'")}
    barriers = conn.execute(
        "SELECT barrier, COUNT(DISTINCT case_id) AS n FROM events WHERE kind = 'barrier'"
        " GROUP BY barrier ORDER BY n DESC, barrier").fetchall()

    return {
        "generated_at": now.isoformat(),
        "totals": {
            "cases": total,
            "completed": len(completed),
            "completed_by_clinic": sum(c["completed_by"] == "clinic" for c in completed),
            "completed_by_patient": sum(c["completed_by"] == "patient" for c in completed),
            "unresolved": sum(c["status"] not in ("completed", "lost") for c in cases),
            "lost": sum(c["status"] == "lost" for c in cases),
            "escalated_now": sum(c["escalation"] != "none" for c in cases),
        },
        "completion_rate": round(len(completed) / total, 3) if total else 0.0,
        "median_days_to_completion": round(statistics.median(durations), 1) if durations else None,
        "share_resolved_without_chw": round(1 - len(escalated_ids) / total, 3) if total else None,
        "top_barriers": [{"barrier": b["barrier"], "cases": b["n"]} for b in barriers],
        "radar": {
            "window_days": RADAR_WINDOW.days, "min_n": RADAR_MIN_N, "barriers": list(RADAR_BARRIERS),
            "facilities": _radar(conn, (now - RADAR_WINDOW).isoformat(), facilities),
        },
        "unreachable": db.unreachable(conn),
        "cases": [
            {"id": c["id"], "status": c["status"], "escalation": c["escalation"],
             "latest_barrier": c["latest_barrier"], "facility": by_id[c["facility_id"]]["name"],
             "visit_date": c["visit_date"], "completed_by": c["completed_by"],
             "referred_at": c["referred_at"]}
            for c in cases
        ],
    }
