from datetime import datetime

import pytest
from twilio.request_validator import RequestValidator

import db
import seed
import templates
from app import create_app

TOKEN = "test-auth-token"
NOW = datetime(2026, 10, 5, 9, 0)  # Monday; FAC-B is open
PATIENT, CHW, CLINIC = seed.DEMO_PATIENT_PHONE, seed.CHWS[0]["phone"], seed.FACILITY_BY_ID["FAC-B"]["phone"]
NEXT_MON = "2026-10-12"
HELP = templates.HELP_SMS.format(phone=CLINIC)  # second SMS after every barrier reply

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


def test_turned_away_then_rebooked_gets_plan_ack_reply_and_counts_on_radar():
    text = "they sent me home, said come back monday"
    client, conn = make(lambda t, today=None: {
        "barrier": "turned_away", "confidence": 0.9, "clinical_flag": False, "fields": {"return_date": NEXT_MON}})
    client.post("/sim/referral")
    sim(client, PATIENT, text)

    assert outbox(client, PATIENT)[-2:] == [f"Thanks! See you at Demo Baraka Clinic on {NEXT_MON}.", HELP]
    assert outbox(client, CLINIC)[-1].startswith(f"R-0142 arriving {NEXT_MON}")
    case = db.get_case(conn, "R-0142")
    assert (case["status"], case["visit_date"], case["escalation"]) == ("visit_scheduled", NEXT_MON, "none")
    barrier = [e for e in db.events(conn, "R-0142") if e["kind"] == "barrier"]
    assert [(e["barrier"], e["facility_id"]) for e in barrier] == [("turned_away", "FAC-B")]


def test_demo_journey_end_to_end():
    client, conn = make()
    client.post("/sim/referral")
    assert db.get_case(conn, "R-0142")["status"] == "contacted"
    assert "referred to Demo Baraka Clinic" in outbox(client, PATIENT)[0]

    # 1. clinic closed (FAC-B open today per posted hours -> hours, radar event)
    assert sim(client, PATIENT, "went there yesterday the gate was locked, nobody there") == "classify"
    assert "Demo Baraka Clinic is open Mon-Fri 8-4" in outbox(client, PATIENT)[-2]
    assert outbox(client, PATIENT)[-1] == HELP
    barrier = [e for e in db.events(conn, "R-0142") if e["kind"] == "barrier"]
    assert [(e["barrier"], e["facility_id"]) for e in barrier] == [("clinic_closed", "FAC-B")]

    # 2. stated plan with a date -> visit set, clinic told
    sim(client, PATIENT, "ok i can go monday")
    case = db.get_case(conn, "R-0142")
    assert (case["status"], case["visit_date"]) == ("visit_scheduled", NEXT_MON)
    assert outbox(client, CLINIC) == [f"R-0142 arriving {NEXT_MON}. Reply Y R-0142 when seen."]

    # 3. symptom -> keyword path, safety SMS, CHW baton; model never called
    assert sim(client, PATIENT, "also my stomach still really hurting") == "clinical"
    assert "A health worker will contact you now" in outbox(client, PATIENT)[-2]
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
    assert outbox(client, PATIENT)[-2:] == ["Thanks, a health worker will follow up.", HELP]


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


# ------------------------------------------------------------ hosting (Render)

def basic(pw):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"demo:{pw}".encode()).decode()}


@pytest.mark.parametrize("headers,status", [({}, 401), (basic("wrong"), 401), (basic("pw123"), 200)],
                         ids=["no password", "wrong password", "right password"])
def test_sim_password_gate(monkeypatch, headers, status):
    monkeypatch.setenv("SIM_PASSWORD", "pw123")
    client, _ = make()
    assert client.get("/sim", headers=headers).status_code == status
    r = client.post("/sim/send", json={"from": PATIENT, "body": "x"}, headers=headers)
    assert r.status_code == (status if status == 401 else 200)


def test_sim_password_does_not_touch_api_or_webhook(monkeypatch):
    monkeypatch.setenv("SIM_PASSWORD", "pw123")
    client, _ = make()
    assert client.get("/api/dashboard").status_code == 200
    assert client.post("/sms", data={}).status_code == 403  # still Twilio-signature gated


def test_seed_demo_on_empty_disk_only(monkeypatch):
    monkeypatch.setenv("SEED_DEMO", "1")
    conn = db.connect()
    create_app(auth_token=TOKEN, conn=conn, sim_mode=True, now_fn=lambda: NOW)
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 40
    create_app(auth_token=TOKEN, conn=conn, sim_mode=True, now_fn=lambda: NOW)  # restart: no reseed
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 40


def test_hosted_deploy_refuses_open_sim(monkeypatch):
    monkeypatch.setenv("REQUIRE_SIM_PASSWORD", "1")
    monkeypatch.delenv("SIM_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="SIM_PASSWORD"):
        create_app(auth_token=TOKEN, conn=db.connect(), sim_mode=True)


# ------------------------------------------------------ live CHW chat, bare replies

def test_claimed_case_is_a_live_chat_both_ways():
    client, conn = escalated_and_claimed()
    assert outbox(client, CHW)[-1].startswith("R-0142 is yours. Texts you send here now go to the patient")
    assert outbox(client, PATIENT)[-1] == "A health worker is now on this chat. Reply here to talk to them."

    assert sim(client, CHW, "Hi, this is Amina. Can you get to the clinic today?") == "relay"
    assert outbox(client, PATIENT)[-1] == "Health worker: Hi, this is Amina. Can you get to the clinic today?"

    assert sim(client, PATIENT, "i can go at 2pm") == "relay"
    assert outbox(client, CHW)[-1] == 'R-0142 patient: "i can go at 2pm"'

    sim(client, CHW, "DONE")  # closes the chat; the patient is back with the agent
    assert db.get_case(conn, "R-0142")["escalation"] == "none"
    assert sim(client, CHW, "anyone there?") == "unparsed"
    assert outbox(client, CHW)[-1].startswith("Commands:")


def test_relay_respects_stop():
    client, conn = escalated_and_claimed()
    sim(client, PATIENT, "STOP")
    before = len(outbox(client, PATIENT))
    sim(client, CHW, "are you ok?")
    assert len(outbox(client, PATIENT)) == before


def test_unclaimed_escalation_still_gets_holding_reply():
    client, conn = make()
    client.post("/sim/referral")
    sim(client, PATIENT, "my chest hurts")
    assert sim(client, PATIENT, "hello?") == "holding"


def test_bare_no_to_intro_asks_for_a_day_not_a_chw():
    client, conn = make()
    client.post("/sim/referral")
    assert sim(client, PATIENT, "no") == "ask_date"
    assert outbox(client, PATIENT)[-1] == "Great! What day will you go to Demo Baraka Clinic? Reply with the day."
    assert outbox(client, CHW) == []
    sim(client, PATIENT, "ok i can go monday")
    assert db.get_case(conn, "R-0142")["visit_date"] == NEXT_MON


def test_bare_yes_to_intro_asks_what_is_stopping_them():
    client, conn = make()
    client.post("/sim/referral")
    assert sim(client, PATIENT, "Yes") == "ask_barrier"
    assert outbox(client, PATIENT)[-1].startswith("What is stopping you from going to Demo Baraka Clinic?")
    assert db.get_case(conn, "R-0142")["escalation"] == "none"


def test_two_non_answers_in_a_row_bring_in_a_chw():
    client, conn = make()
    client.post("/sim/referral")
    sim(client, PATIENT, "ok")      # re-asked once
    assert outbox(client, PATIENT)[-1].endswith("Reply YES or NO.")
    sim(client, PATIENT, "ok")      # still no answer: a human takes it
    assert db.get_case(conn, "R-0142")["escalation"] == "non_clinical"
    assert outbox(client, CHW)[-1].startswith("NON-CLINICAL R-0142")


def test_unclear_message_gets_one_clarifying_question():
    unclear = {"barrier": "unknown", "confidence": 0.9, "clinical_flag": False}
    client, conn = make(classify_fn=lambda text, today=None: unclear)
    client.post("/sim/referral")
    sim(client, PATIENT, "my cousin said the thing")
    assert outbox(client, PATIENT)[-1].startswith("Sorry, I didn't get that.")
    assert db.get_case(conn, "R-0142")["escalation"] == "none"
    sim(client, PATIENT, "the thing with the paper")
    assert db.get_case(conn, "R-0142")["escalation"] == "non_clinical"


def test_model_outage_still_escalates_at_once():
    def down(text, today=None):
        raise RuntimeError("down")
    client, conn = make(classify_fn=down)
    client.post("/sim/referral")
    sim(client, PATIENT, "nimeshindwa kufika")
    assert db.get_case(conn, "R-0142")["escalation"] == "clinical"


def test_home_page_links():
    client, _ = make()
    page = client.get("/").get_data(as_text=True)
    for href in ('href="/sim"', 'href="/referrals/new"', "pixel-perfect-showcase-4501.lovable.app",
                 "github.com/oscar598/sms-voice"):
        assert href in page
