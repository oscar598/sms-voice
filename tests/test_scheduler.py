from datetime import datetime, timedelta

import pytest

import db
import scheduler
import seed
from app import create_app, handle_inbound, start_referral

T0 = datetime(2026, 10, 5, 9, 0)  # Monday
H = timedelta(hours=1)
D = timedelta(days=1)
PATIENT, CHW = seed.DEMO_PATIENT_PHONE, seed.CHWS[0]["phone"]
CLINIC = seed.FACILITY_BY_ID["FAC-B"]["phone"]


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, to, body):
        self.sent.append((to, body))
        return f"SIM-{len(self.sent)}"

    def to(self, phone):
        return [b for t, b in self.sent if t == phone]


@pytest.fixture
def env():
    conn, out = db.connect(), Outbox()
    start_referral(conn, out, dict(seed.DEMO_CASE), T0)
    return conn, out


def tick(env, at):
    conn, out = env
    return scheduler.tick(conn, out, at, seed)


def inbound(env, phone, body, at):
    conn, out = env
    return handle_inbound(conn, out, phone, body, at, classify_fn=lambda t, d: (_ for _ in ()).throw(AssertionError))


def case(env):
    return db.get_case(env[0], "R-0142")


def schedule_visit(env, visit="2026-10-12"):
    db.update_case(env[0], "R-0142", status="visit_scheduled", visit_date=visit, awaiting="free_text")
    env[0].commit()


# ------------------------------------------------------------ reminders

def test_two_reminders_then_chw(env):
    assert tick(env, T0 + 47 * H) == []
    assert tick(env, T0 + 48 * H) == [("R-0142", "reminder")]
    assert tick(env, T0 + 95 * H) == []                          # 48 h from the reminder, not the intro
    assert tick(env, T0 + 96 * H) == [("R-0142", "reminder")]
    assert tick(env, T0 + 144 * H) == [("R-0142", "no_response")]
    assert case(env)["escalation"] == "non_clinical"
    assert env[1].to(CHW)[-1].startswith("NON-CLINICAL R-0142")
    assert tick(env, T0 + 30 * D) == []                          # stuck stays stuck (R6)


def test_patient_reply_restarts_the_wait(env):
    inbound(env, PATIENT, "hello?", T0 + 40 * H)                 # holding reply goes out at +40 h
    assert tick(env, T0 + 50 * H) == []


def test_opted_out_patient_gets_no_reminders_and_is_lost_after_7_days(env):
    inbound(env, PATIENT, "STOP", T0 + 1 * H)
    assert tick(env, T0 + 1 * H + 7 * D - H) == []            # the week runs from STOP
    assert tick(env, T0 + 1 * H + 7 * D) == [("R-0142", "lost")]
    assert len(env[1].to(PATIENT)) == 1                          # only the intro


def test_waiting_for_a_distant_visit_is_not_lost(env):
    schedule_visit(env, "2026-10-20")
    assert tick(env, T0 + 10 * D) == []
    assert case(env)["status"] == "visit_scheduled"


# ------------------------------------------------------------- follow-up

def test_followup_goes_out_the_day_after_the_visit(env):
    schedule_visit(env)
    assert tick(env, datetime(2026, 10, 12, 23, 0)) == []
    assert tick(env, datetime(2026, 10, 13, 8, 0)) == [("R-0142", "followup")]
    c = case(env)
    assert (c["status"], c["awaiting"]) == ("follow_up", "yes_no")
    assert env[1].to(PATIENT)[-1] == "Did you get seen at Demo Baraka Clinic? Reply YES or NO."
    assert env[1].to(CLINIC)[-1] == "Was R-0142 seen? Reply Y R-0142 or N R-0142."


FU = datetime(2026, 10, 13, 8, 0)

# (id, [(phone, body, hours after follow-up)], tick hours, status, completed_by, escalation)
FOLLOWUP_CASES = [
    ("clinic Y", [(CLINIC, "Y R-0142", 1)], 49, "completed", "clinic", "none"),
    ("patient YES, clinic silent 48 h", [(PATIENT, "yes", 1)], 49, "completed", "patient", "none"),
    ("patient YES, clinic N", [(PATIENT, "yes", 1), (CLINIC, "N R-0142", 2)], 3, "follow_up", None, "non_clinical"),
    ("clinic N, then patient YES", [(CLINIC, "N R-0142", 1), (PATIENT, "yes", 2)], 3, "follow_up", None, "non_clinical"),
    ("both silent 48 h", [], 48, "follow_up", None, "non_clinical"),
    ("clinic N only: no rule, stays", [(CLINIC, "N R-0142", 1)], 49, "follow_up", None, "none"),
]


@pytest.mark.parametrize("cid,msgs,tick_h,status,by,esc", FOLLOWUP_CASES, ids=[c[0] for c in FOLLOWUP_CASES])
def test_followup_resolution(env, cid, msgs, tick_h, status, by, esc):
    schedule_visit(env)
    tick(env, FU)
    for phone, body, h in msgs:
        inbound(env, phone, body, FU + h * H)
    tick(env, FU + tick_h * H)
    c = case(env)
    assert (c["status"], c["completed_by"], c["escalation"]) == (status, by, esc)


def test_patient_no_asks_what_got_in_the_way(env):
    schedule_visit(env)
    tick(env, FU)
    assert inbound(env, PATIENT, "no", FU + H) == "no"
    c = case(env)
    assert (c["status"], c["awaiting"]) == ("barrier_found", "free_text")
    assert env[1].to(PATIENT)[-1] == "Sorry to hear that. What got in the way? Reply here."
    assert tick(env, FU + 49 * H) == [("R-0142", "reminder")]   # back in the reminder loop


# ------------------------------------------------------- fast-forward (sim)

def test_fast_forward_drives_followup_in_sim():
    conn = db.connect()
    model = {"ok i can go monday": {"barrier": "plan_ack", "confidence": 0.9, "clinical_flag": False,
                                    "fields": {"return_date": "2026-10-12"}}}
    app = create_app(auth_token="t", conn=conn, sim_mode=True, now_fn=lambda: T0,
                     classify_fn=lambda text, today: model[text])
    client = app.test_client()
    client.post("/sim/referral")
    client.post("/sim/send", json={"from": PATIENT, "body": "ok i can go monday"})
    r = client.post("/sim/fast-forward", json={"days": 8}).get_json()
    assert ["R-0142", "followup"] in r["applied"]
    client.post("/sim/send", json={"from": CLINIC, "body": "Y R-0142"})
    assert db.get_case(conn, "R-0142")["status"] == "completed"


# ------------------------------------------------------------ STOP (T9, R5d)

def test_opted_out_patient_gets_no_followup_but_clinic_does(env):
    schedule_visit(env)
    inbound(env, PATIENT, "STOP", T0 + H)
    assert tick(env, FU) == [("R-0142", "followup")]
    assert env[1].to(PATIENT) == [env[1].to(PATIENT)[0]]          # only the intro, before STOP
    assert env[1].to(CLINIC)[-1].startswith("Was R-0142 seen?")


def test_send_path_refuses_opted_out_patient(env):
    from app import send_sms
    conn, out = env
    db.update_case(conn, "R-0142", opted_out=1)
    before = len(out.sent)
    assert send_sms(conn, out, "R-0142", PATIENT, "any feature's SMS", T0 + D) is False
    assert len(out.sent) == before
    assert db.last_event(conn, "R-0142", ["suppressed_opted_out"])["payload_json"] == f'{{"to": "{PATIENT}"}}'
    # Staff phones on the same case are unaffected.
    assert send_sms(conn, out, "R-0142", CHW, "baton", T0 + D) is True
