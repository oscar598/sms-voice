"""Referral-agent demo data: its clinics (from clinics.json), the CHW, the demo case
and 40 synthetic history cases. Placeholder phone numbers throughout.
"""

import clinics

FACILITIES = clinics.for_program("referral")
FACILITY_BY_ID = {f["id"]: f for f in FACILITIES}
AREAS = sorted({f["area"] for f in FACILITIES})
# Clinical SMS: "call 911 or go to <this clinic>" - the first one offering emergency care.
EMERGENCY = next({"name": f["name"]} for f in FACILITIES if "emergency" in f["services"])
CHWS = [{"id": "CHW-1", "name": "Demo CHW Amina", "phone": "+254700000201"}]

DEMO_PATIENT_PHONE = "+254700000301"
DEMO_CASE = {"id": "R-0142", "patient_phone": DEMO_PATIENT_PHONE, "patient_area": "Embakasi",
             "service": "lab", "facility_id": "FAC-B"}


# ------------------------------------------------------------------ demo history
# 40 synthetic cases over the last 7 days so the dashboard has something to show.
# Clinic B (FAC-B) is the one the radar flags: 6 of its 11 cases report it closed
# or turning people away. Each row: (facility, service, days_ago, barrier, outcome)
# outcome: completed_clinic | completed_patient | open | escalated | lost
_HISTORY = (
    [("FAC-B", "lab", d, b, o) for d, b, o in [
        (6.5, "clinic_closed", "open"), (6.0, "clinic_closed", "escalated"),
        (5.5, "turned_away", "completed_clinic"), (5.0, "clinic_closed", "open"),
        (4.0, "turned_away", "open"), (3.5, "clinic_closed", "lost"),
        (3.0, "transport", "completed_clinic"), (2.5, "cost", "open"),
        (2.0, "scheduling", "completed_patient"), (1.5, "missing_documents", "open"),
        (1.0, None, "open")]]
    + [("FAC-A", s, d, b, o) for s, d, b, o in [
        ("antenatal", 6.8, "transport", "completed_clinic"), ("hiv", 6.2, None, "completed_clinic"),
        ("lab", 5.8, "cost", "completed_patient"), ("antenatal", 5.2, "fear_confusion", "completed_clinic"),
        ("hiv", 4.6, "missing_documents", "completed_clinic"), ("lab", 4.1, "clinic_closed", "open"),
        ("antenatal", 3.6, "scheduling", "completed_clinic"), ("hiv", 3.1, "transport", "open"),
        ("lab", 2.4, "language", "escalated"), ("antenatal", 1.8, None, "completed_patient"),
        ("hiv", 1.2, "transport", "open"), ("lab", 0.6, None, "open")]]
    + [("FAC-C", "eye", d, b, o) for d, b, o in [
        (6.6, "transport", "completed_clinic"), (5.4, "cost", "open"),
        (4.8, "scheduling", "completed_clinic"), (3.9, "fear_confusion", "escalated"),
        (2.9, None, "completed_clinic"), (2.2, "transport", "open"),
        (1.4, "missing_documents", "open"), (0.8, None, "open")]]
    + [("FAC-D", s, d, b, o) for s, d, b, o in [
        ("maternity", 6.9, "transport", "completed_clinic"), ("specialist", 6.1, "cost", "completed_clinic"),
        ("lab", 5.3, None, "completed_clinic"), ("maternity", 4.4, "scheduling", "completed_patient"),
        ("specialist", 3.8, "wrong_facility", "open"), ("maternity", 3.2, "transport", "completed_clinic"),
        ("lab", 2.6, "fear_confusion", "escalated"), ("specialist", 1.6, None, "open"),
        ("maternity", 0.9, "cost", "open")]]
)


def seed_history(conn, now):
    """Insert the 40 demo cases with their events. Idempotent per case id."""
    from datetime import timedelta

    import db

    for i, (fac_id, service, days_ago, barrier, outcome) in enumerate(_HISTORY, start=1):
        cid = f"R-{i:04d}"
        if db.get_case(conn, cid):
            continue
        # Invalid Kenyan range on purpose: these must never reach a real phone.
        phone = f"+2540000{i:05d}"
        t0 = now - timedelta(days=days_ago)
        at = lambda hours: (t0 + timedelta(hours=hours)).isoformat()  # noqa: E731
        db.add_case(conn, id=cid, patient_phone=phone, patient_area=FACILITY_BY_ID[fac_id]["area"],
                    service=service, facility_id=fac_id, referred_at=t0.isoformat(), status="contacted",
                    awaiting=None)  # not awaiting a reply, so no timer ever texts seeded cases
        db.log_event(conn, cid, at(0), "agent", "sent", to=phone, sid=f"SEED-{i}")
        if barrier:
            db.log_event(conn, cid, at(3), "patient", "inbound", body="(seeded)", route="classify")
            db.log_event(conn, cid, at(3), "agent", "barrier", barrier=barrier, facility_id=fac_id, reason="seed")
            db.update_case(conn, cid, latest_barrier=barrier, status="action_taken")
        if outcome.startswith("completed"):
            done_after = 24 * min(days_ago * 0.8, 1 + (i % 5))  # 1-5 days, never in the future
            db.complete_case(conn, cid, outcome.split("_")[1], at(done_after))
        elif outcome == "escalated":
            db.update_case(conn, cid, escalation="non_clinical", awaiting=None)
            db.log_event(conn, cid, at(4), "agent", "escalated", level="non_clinical")
        elif outcome == "lost":
            db.update_case(conn, cid, status="lost", awaiting=None)
            db.log_event(conn, cid, at(24 * 3), "system", "timer", rule="lost")
    conn.commit()


if __name__ == "__main__":
    import argparse
    from datetime import datetime

    import db

    ap = argparse.ArgumentParser(description="Load 40 synthetic demo cases into a database.")
    ap.add_argument("--db", default="sim.db")
    args = ap.parse_args()
    conn = db.connect(args.db)
    seed_history(conn, datetime.now())
    print(f"seeded {len(_HISTORY)} demo cases into {args.db}")
