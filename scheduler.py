"""Time-based rules: follow-up, reminders, follow-up timeout, lost.

`tick()` is called at the start of every request in the same thread (D3),
so the /sim page and dashboard polling drive it; there is no background
thread. Rules come from the approved design doc; stuck (escalated) cases are
left alone by user direction (R6).
"""

from datetime import date, datetime, timedelta

import db
import templates

FOLLOWUP_DELAY = timedelta(days=1)  # after visit_date
REPLY_WAIT = timedelta(hours=48)    # reminder spacing and follow-up timeout
MAX_REMINDERS = 2
LOST_AFTER = timedelta(days=7)      # from the last SMS to the patient

OPEN = ("contacted", "barrier_found", "action_taken")


def _ts(event):
    return datetime.fromisoformat(event["ts"]) if event else None


def _escalate(conn, send, case, now, why, data):
    # Imported here to avoid an import cycle (app imports scheduler).
    from app import _escalate as escalate
    db.log_event(conn, case["id"], now.isoformat(), "system", "timer", rule=why)
    escalate(conn, send, case, "non_clinical", f"[{why}]", now, data)


def tick(conn, send, now, data):
    """Apply every due rule once. Returns a list of (case_id, rule) applied."""
    from app import send_sms

    done = []
    cases = [dict(r) for r in conn.execute(
        "SELECT * FROM cases WHERE status NOT IN ('completed', 'lost') AND escalation = 'none'")]
    for case in cases:
        cid, phone = case["id"], case["patient_phone"]
        fac = data.FACILITY_BY_ID[case["facility_id"]]
        last_sent = db.last_event(conn, cid, ["sent"], to=phone)
        last_inbound = db.last_event(conn, cid, ["inbound"], actor="patient")
        quiet_since = _ts(last_sent) if last_sent and (
            not last_inbound or last_inbound["id"] < last_sent["id"]) else None

        # 1. Follow-up the day after the visit date: ask the patient and the clinic.
        if case["status"] == "visit_scheduled" and case["visit_date"] and \
                now.date() >= date.fromisoformat(case["visit_date"]) + FOLLOWUP_DELAY:
            db.update_case(conn, cid, status="follow_up", awaiting="yes_no")
            db.log_event(conn, cid, now.isoformat(), "system", "followup_sent")
            if not case["opted_out"]:
                send_sms(conn, send, cid, phone,
                         templates.render(templates.PATIENT, "followup", name=fac["name"]), now)
            send_sms(conn, send, cid, fac["phone"],
                     templates.render(templates.CLINIC, "followup", case_id=cid), now)
            done.append((cid, "followup"))
            continue

        # 2. Follow-up timeout: clinic silent 48 h after asking.
        if case["status"] == "follow_up":
            asked = _ts(db.last_event(conn, cid, ["followup_sent"]))
            if asked and now - asked >= REPLY_WAIT:
                patient_yes = db.last_event(conn, cid, ["patient_yes"])
                clinic_no = db.last_event(conn, cid, ["clinic_not_seen"])
                # YES + clinic N is escalated when the second reply arrives (app.py),
                # so an escalation-free case here never has both.
                if patient_yes:
                    db.complete_case(conn, cid, "patient", now.isoformat())
                    done.append((cid, "completed_by_patient"))
                elif not patient_yes and not clinic_no and not db.last_event(conn, cid, ["patient_no"]):
                    _escalate(conn, send, case, now, "followup_unanswered", data)
                    done.append((cid, "followup_unanswered"))
                # Patient silent + clinic N: no rule (R6a not adopted); stays as is.
            continue

        # 3. No reply to an open question: up to two reminders, then a CHW.
        if case["status"] in OPEN and case["awaiting"] and not case["opted_out"] and quiet_since \
                and now - quiet_since >= REPLY_WAIT:
            if case["reminder_count"] < MAX_REMINDERS:
                db.update_case(conn, cid, reminder_count=case["reminder_count"] + 1,
                               awaiting="barrier_q")  # the reminder asks "is anything stopping you?"
                send_sms(conn, send, cid, phone,
                         templates.render(templates.PATIENT, "reminder", name=fac["name"]), now)
                done.append((cid, "reminder"))
            else:
                _escalate(conn, send, case, now, "no_response", data)
                done.append((cid, "no_response"))
            continue

        # 4. Lost: a week with no patient reply. An opted-out patient's last message
        # is usually STOP itself, so their week runs from the last message either way.
        if case["opted_out"]:
            quiet_since = max(filter(None, (_ts(last_sent), _ts(last_inbound))), default=None)
        if case["status"] in OPEN and quiet_since and now - quiet_since >= LOST_AFTER:
            db.update_case(conn, cid, status="lost", awaiting=None)
            db.log_event(conn, cid, now.isoformat(), "system", "timer", rule="lost")
            done.append((cid, "lost"))
    conn.commit()
    return done
