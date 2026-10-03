from datetime import datetime

import pytest
from twilio.request_validator import RequestValidator

import db
import seed
from app import create_app

TOKEN = "test-auth-token"
NOW = datetime(2026, 10, 5, 9, 0)  # Monday; FAC-B is open
PATIENT, CHW, CLINIC = seed.DEMO_PATIENT_PHONE, seed.CHWS[0]["phone"], seed.FACILITY_BY_ID["FAC-B"]["phone"]
NEXT_MON = "2026-10-12"

# Stand-in for the model: message -> labelled output (the real call is classify.py).
MODEL = {
    "went there yesterday the gate was locked, nobody there":
        {"barrier": "clinic_closed", "confidence": 0.86, "clinical_flag": False},
    "ok i can go monday":
        {"barrier": "plan_ack", "confidence": 0.9, "clinical_flag": False,
         "fields": {"return_date": NEXT_MON}},
}


def fake_model(text, today=None):
    return MODEL[text]


def make(classify_fn=fake_model):
    conn = db.connect()
    app = create_app(auth_token=TOKEN, conn=conn, sim_mode=True,
                     classify_fn=classify_fn, now_fn=lambda: NOW)
    return app.test_client(), conn


def sim(client, phone, body):
    return client.post("/sim/send", json={"from": phone, "body": body}).get_json()["route"]


def sms(client, phone, body):
    form = {"From": phone, "Body": body}
    sig = RequestValidator(TOKEN).compute_signature("http://localhost/sms", form)
    return client.post("/sms", data=form, headers={"X-Twilio-Signature": sig})


def outbox(client, phone):
    msgs = client.get("/sim/transcript").get_json()["messages"]
    return [m["body"] for m in msgs if m["dir"] == "out" and m["phone"] == phone]


def test_demo_journey_end_to_end():
    client, conn = make()
    client.post("/sim/referral")
    assert db.get_case(conn, "R-0142")["status"] == "contacted"
    assert "referred to Demo Baraka Clinic" in outbox(client, PATIENT)[0]

    # 1. clinic closed (FAC-B open today per posted hours -> hours, radar event)
    assert sim(client, PATIENT, "went there yesterday the gate was locked, nobody there") == "classify"
    assert "Demo Baraka Clinic is open Mon-Fri 8-4" in outbox(client, PATIENT)[-1]
    barrier = [e for e in db.events(conn, "R-0142") if e["kind"] == "barrier"]
    assert [(e["barrier"], e["facility_id"]) for e in barrier] == [("clinic_closed", "FAC-B")]

    # 2. stated plan with a date -> visit set, clinic told
    sim(client, PATIENT, "ok i can go monday")
    case = db.get_case(conn, "R-0142")
    assert (case["status"], case["visit_date"]) == ("visit_scheduled", NEXT_MON)
    assert outbox(client, CLINIC) == [f"R-0142 arriving {NEXT_MON}. Reply Y R-0142 when seen."]

    # 3. symptom -> keyword path, safety SMS, CHW baton; model never called
    assert sim(client, PATIENT, "also my stomach still really hurting") == "clinical"
    assert "A health worker will contact you now" in outbox(client, PATIENT)[-1]
    assert outbox(client, CHW)[-1].startswith("CLINICAL R-0142")
    assert db.get_case(conn, "R-0142")["escalation"] == "clinical"

    # 4. while escalated, chatter gets the holding reply (R6)
    assert sim(client, PATIENT, "hello?") == "holding"
    assert "replies are slow" in outbox(client, PATIENT)[-1]

    # 5. CHW claims, then closes; the Monday visit survives
    assert sim(client, CHW, "1 R-0142") == "chw_claim"
    assert db.get_case(conn, "R-0142")["claimed_by_chw"] == "CHW-1"
    sim(client, CHW, "DONE R-0142")
    case = db.get_case(conn, "R-0142")
    assert (case["escalation"], case["status"]) == ("none", "visit_scheduled")

    # 6. clinic confirms -> completed by clinic
    assert sim(client, CLINIC, "Y R-0142") == "clinic_seen"
    case = db.get_case(conn, "R-0142")
    assert (case["status"], case["completed_by"]) == ("completed", "clinic")


def strip(events):
    return [{k: e[k] for k in ("case_id", "actor", "kind", "barrier", "facility_id", "payload_json")}
            for e in events]


@pytest.mark.parametrize("body", [
    "went there yesterday the gate was locked, nobody there",
    "my chest hurts",
    "STOP",
])
def test_sim_and_sms_produce_identical_events(body):
    runs = []
    for send in (sim, sms):
        client, conn = make()
        client.post("/sim/referral")
        send(client, PATIENT, body)
        runs.append(strip(db.events(conn)))
    assert runs[0] == runs[1]


def test_classifier_outage_routes_free_text_to_urgent_chw():
    from classify import ClassifierUnavailable

    def down(text, today=None):
        raise ClassifierUnavailable("APITimeoutError")
    client, conn = make(classify_fn=down)
    client.post("/sim/referral")
    sim(client, PATIENT, "sina doh ya mat")
    case = db.get_case(conn, "R-0142")
    assert case["escalation"] == "clinical"  # D6: outage uses clinical timing
    assert [e["kind"] for e in db.events(conn, "R-0142")].count("model_error") == 1
    assert outbox(client, PATIENT)[-1] == "Thanks, a health worker will follow up."


def test_stop_silences_everything_until_start():
    client, conn = make()
    client.post("/sim/referral")
    sent_before = len(outbox(client, PATIENT))
    sim(client, PATIENT, "STOP")
    assert sim(client, PATIENT, "my chest hurts") == "ignore"
    assert len(outbox(client, PATIENT)) == sent_before
    assert outbox(client, CHW) == []
    assert sim(client, PATIENT, "START") == "opt_in"


def test_unknown_sender_and_bare_staff_reply():
    client, conn = make()
    client.post("/sim/referral")
    assert sim(client, "+254799999999", "hi") == "no_case"
    assert sim(client, CLINIC, "Y") == "which"  # nothing scheduled yet
    assert outbox(client, CLINIC)[-1].startswith("Which case?")


def test_sim_routes_absent_outside_sim_mode():
    app = create_app(auth_token=TOKEN, conn=db.connect(), sender=lambda to, body: "SM", sim_mode=False)
    client = app.test_client()
    assert client.post("/sim/send", json={"from": PATIENT, "body": "x"}).status_code == 404
    assert client.get("/sim").status_code == 404


# ------------------------------------------------------------ CHW / clinic replies

def escalated_and_claimed():
    client, conn = make()
    client.post("/sim/referral")
    sim(client, PATIENT, "my chest hurts")
    sim(client, CHW, "1 R-0142")
    return client, conn


@pytest.mark.parametrize("cmd", ["DONE R-0142", "done"], ids=["with id", "bare DONE finds the claimed case"])
def test_chw_done_is_confirmed(cmd):
    client, conn = escalated_and_claimed()
    assert sim(client, CHW, cmd) == "chw_done"
    assert outbox(client, CHW)[-1] == "R-0142 closed. Automated follow-up for the patient resumes."
    assert db.get_case(conn, "R-0142")["escalation"] == "none"


def test_chw_lost_is_confirmed():
    client, conn = escalated_and_claimed()
    sim(client, CHW, "LOST R-0142")
    assert outbox(client, CHW)[-1].startswith("R-0142 marked lost")
    assert db.get_case(conn, "R-0142")["status"] == "lost"


def test_chw_done_on_case_with_nothing_open():
    client, conn = escalated_and_claimed()
    sim(client, CHW, "DONE R-0142")
    sim(client, CHW, "DONE R-0142")
    assert outbox(client, CHW)[-1] == "R-0142 has nothing waiting for a health worker."


@pytest.mark.parametrize("phone,expected", [
    (CHW, "Commands: 1 R-0142 to take a case"),
    (CLINIC, "Reply Y R-0142 if the patient was seen"),
], ids=["chw free text", "clinic free text"])
def test_staff_free_text_gets_the_commands(phone, expected):
    client, conn = make()
    client.post("/sim/referral")
    assert sim(client, phone, "hellO?") == "unparsed"
    assert outbox(client, phone)[-1].startswith(expected)
