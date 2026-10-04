from datetime import date, timedelta

import pytest

import core

MON = date(2026, 10, 5)  # a Monday
SUN = date(2026, 10, 4)
WEEKDAYS = {d: "08:00-17:00" for d in range(5)}

FACILITIES = [
    {"id": "FAC-A", "name": "Demo Mto Health Centre", "area": "Kayole",
     "services": ["antenatal", "hiv", "lab"], "hours": {**WEEKDAYS, 5: "08:00-12:00"},
     "hours_text": "Mon-Fri 8-5, Sat 8-12", "address": "Off Spine Rd, Kayole",
     "phone": "+254700000101", "required_docs": ["referral letter", "national id", "clinic book"],
     "cost_note": "Consultation free; lab KES 200.", "transport_note": "Matatu 34 to Stage 2"},
    {"id": "FAC-B", "name": "Demo Baraka Clinic", "area": "Embakasi",
     "services": ["outpatient", "tb", "lab"], "hours": dict(WEEKDAYS),
     "hours_text": "Mon-Fri 8-4", "address": "Embakasi Rd", "phone": "+254700000102",
     "required_docs": ["referral letter", "national id"], "cost_note": "",
     "transport_note": "Matatu 33"},
    {"id": "FAC-C", "name": "Demo Upendo Eye Unit", "area": "Westlands",
     "services": ["eye"], "hours": {1: "09:00-15:00", 3: "09:00-15:00"},
     "hours_text": "Tue & Thu 9-3", "address": "Westlands", "phone": "+254700000103",
     "required_docs": ["referral letter"], "cost_note": "Free screening.",
     "transport_note": "Matatu 23"},
    {"id": "FAC-D", "name": "Demo Faraja Hospital", "area": "Kibera",
     "services": ["specialist", "maternity", "emergency", "lab"],
     "hours": {d: "00:00-24:00" for d in range(7)}, "hours_text": "24 h emergency",
     "address": "Kibera Dr", "phone": "+254700000104",
     "required_docs": ["referral letter", "national id"], "cost_note": "SHA accepted.",
     "transport_note": "Matatu 8"},
]
BY_ID = {f["id"]: f for f in FACILITIES}
AREAS = ["Kayole", "Embakasi", "Westlands", "Kibera"]
EMERGENCY = {"name": "Demo Faraja Hospital"}


# ------------------------------------------------------------------ route

ACTIVE = {"escalation": "none"}
ROUTE_CASES = [
    # (id, body, role, case, open_prompt, kind, case_id)
    ("symptom before anything", "stomach still really hurting", "patient", ACTIVE, "free_text", "clinical", None),
    ("symptom inside a NO reply", "NO, my chest hurts", "patient", ACTIVE, "yes_no", "clinical", None),
    ("swahili symptom", "bado nina maumivu ya tumbo", "patient", ACTIVE, "free_text", "clinical", None),
    ("word boundary, not clinical", "i sell chestnuts near the clinic", "patient", ACTIVE, "free_text", "classify", None),
    ("symptom on escalated case", "chest pain again", "patient", {"escalation": "clinical"}, None, "clinical", None),
    ("exact yes", "Yes", "patient", ACTIVE, "yes_no", "yes", None),
    ("swahili yes", "ndio", "patient", ACTIVE, "yes_no", "yes", None),
    ("exact no with punctuation", "no.", "patient", ACTIVE, "yes_no", "no", None),
    ("longer no goes to model", "no, the clinic was shut", "patient", ACTIVE, "yes_no", "classify", None),
    ("stop", "STOP", "patient", ACTIVE, "free_text", "opt_out", None),
    ("opted out: symptom is silent", "chest pain", "patient", {"opted_out": True}, None, "ignore", None),
    ("opted out: start", "start", "patient", {"opted_out": True}, None, "opt_in", None),
    ("stuck: escalated", "hello?", "patient", {"escalation": "non_clinical"}, "free_text", "holding", None),
    ("stuck: no open prompt", "hello?", "patient", ACTIVE, None, "holding", None),
    ("free text to model", "sina doh ya mat", "patient", ACTIVE, "free_text", "classify", None),
    ("stuck but claimed: live chat", "hello?", "patient",
     {"escalation": "non_clinical", "claimed_by_chw": "CHW-1"}, "free_text", "relay", None),
    ("yes to 'anything stopping you'", "yes", "patient", ACTIVE, "barrier_q", "ask_barrier", None),
    ("no to 'anything stopping you'", "No.", "patient", ACTIVE, "barrier_q", "ask_date", None),
    ("swahili no to 'anything stopping you'", "hapana", "patient", ACTIVE, "barrier_q", "ask_date", None),
    ("ok to a yes/no question", "ok", "patient", ACTIVE, "barrier_q", "ask_again", None),
    ("ok after advice, no visit yet", "sawa", "patient", ACTIVE, "free_text", "ask_date", None),
    ("ok after advice, visit set", "thanks", "patient",
     {"escalation": "none", "visit_date": "2026-10-12"}, "free_text", "ack_visit", None),
    ("bare no after advice", "no", "patient", ACTIVE, "free_text", "ask_barrier", None),
    ("ok on the follow-up goes to model", "ok", "patient", ACTIVE, "yes_no", "classify", None),
    ("longer yes goes to model", "yes the bus is too expensive", "patient", ACTIVE, "barrier_q", "classify", None),
    ("chw claim", "1 R-0142", "chw", None, None, "chw_claim", "R-0142"),
    ("chw claim, loose id", "1 0142", "chw", None, None, "chw_claim", "R-0142"),
    ("chw bare claim", "1", "chw", None, None, "chw_claim", None),
    ("chw done", "DONE r142", "chw", None, None, "chw_done", "R-0142"),
    ("chw lost", "lost R-7", "chw", None, None, "chw_lost", "R-0007"),
    ("chw chatter", "ok will call", "chw", None, None, "unparsed", None),
    ("clinic seen", "Y R-0142", "clinic", None, None, "clinic_seen", "R-0142"),
    ("clinic not seen", "n 142", "clinic", None, None, "clinic_not_seen", "R-0142"),
    ("clinic chatter", "yes she came", "clinic", None, None, "unparsed", None),
]


@pytest.mark.parametrize("cid,body,role,case,prompt,kind,ref", ROUTE_CASES, ids=[c[0] for c in ROUTE_CASES])
def test_route(cid, body, role, case, prompt, kind, ref):
    r = core.route(body, role, case, prompt)
    assert (r.kind, r.case_id) == (kind, ref)


# ----------------------------------------------------------------- guards

def raw(barrier="transport", conf=0.9, **kw):
    return {"barrier": barrier, "confidence": conf, "clinical_flag": False, **kw}


GUARD_CASES = [
    # (id, raw, error, label, urgent, reason)
    ("api error is urgent (D6)", None, True, "unknown", True, "model_error"),
    ("non-dict output", "transport", False, "unknown", True, "model_error"),
    ("label outside enum", raw("rash"), False, "unknown", True, "model_error"),
    ("confidence out of range", raw(conf=1.5), False, "unknown", True, "model_error"),
    ("clinical flag beats label", raw(clinical_flag=True), False, "clinical_symptom", True, "clinical"),
    ("clinical at low confidence", raw("clinical_symptom", 0.2), False, "clinical_symptom", True, "clinical"),
    ("below floor", raw(conf=0.69), False, "unknown", False, "low_confidence"),
    ("at floor", raw(conf=0.7), False, "transport", False, "ok"),
    ("two admin barriers", raw(also_mentions=["clinic_closed"]), False, "unknown", False, "multi_barrier"),
    ("non-admin mention ignored", raw(also_mentions=["plan_ack"]), False, "transport", False, "ok"),
]


@pytest.mark.parametrize("cid,r,error,label,urgent,reason", GUARD_CASES, ids=[c[0] for c in GUARD_CASES])
def test_guards(cid, r, error, label, urgent, reason):
    c = core.apply_guards(r, MON, AREAS, BY_ID["FAC-A"], error=error)
    assert (c.label, c.urgent, c.reason) == (label, urgent, reason)


NEXT_MON = {"return_date": "2026-10-12"}
RESCHEDULE_CASES = [
    # (id, raw, label, reason, logged barrier)
    ("turned away, new date -> plan_ack", raw("turned_away", fields=NEXT_MON), "plan_ack", "rescheduled", "turned_away"),
    ("plan_ack + turned_away mention", raw("plan_ack", also_mentions=["turned_away"], fields=NEXT_MON),
     "plan_ack", "rescheduled", "turned_away"),
    ("turned away, no date", raw("turned_away"), "turned_away", "ok", "turned_away"),
    ("turned away, date out of window", raw("turned_away", fields={"return_date": "2027-01-01"}),
     "turned_away", "ok", "turned_away"),
    ("plain plan_ack logs nothing", raw("plan_ack", fields=NEXT_MON), "plan_ack", "ok", None),
]


@pytest.mark.parametrize("cid,r,label,reason,logged", RESCHEDULE_CASES, ids=[c[0] for c in RESCHEDULE_CASES])
def test_turned_away_then_rescheduled_is_plan_ack(cid, r, label, reason, logged):
    c = core.apply_guards(r, MON, AREAS, BY_ID["FAC-B"])
    assert (c.label, c.reason, c.logged_barrier) == (label, reason, logged)


def test_rescheduled_reply_is_plan_ack_but_counts_on_radar():
    cls = core.apply_guards(raw("turned_away", fields=NEXT_MON), MON, AREAS, BY_ID["FAC-B"])
    p = core.decide(cls, {"id": "R-0142", "service": "tb", "area": "Embakasi"}, BY_ID["FAC-B"],
                    FACILITIES, MON, EMERGENCY)
    assert p.patient[0] == "plan_ack" and p.set_visit_date == "2026-10-12"
    assert p.clinic[0] == "FAC-B" and p.radar_event == "FAC-B" and p.escalate is None


FIELD_CASES = [
    # (id, raw fields, expected validated fields)
    ("date in window", {"return_date": (MON + timedelta(days=30)).isoformat()}, {"return_date": "2026-11-04"}),
    ("date too far", {"return_date": (MON + timedelta(days=31)).isoformat()}, {}),
    ("date in past", {"return_date": "2026-10-01"}, {}),
    ("date not iso", {"return_date": "monday"}, {}),
    ("supported language", {"language": "sw"}, {"language": "sw"}),
    ("unsupported language", {"language": "fr"}, {}),
    ("area fuzzy match", {"area": "kayol"}, {"area": "Kayole"}),
    ("area unknown", {"area": "Mombasa"}, {}),
    ("doc in facility list", {"missing_doc": "Referral Letter"}, {"missing_doc": "referral letter"}),
    ("doc not required there", {"missing_doc": "passport"}, {}),
    ("fields not a dict", "oops", {}),
]


@pytest.mark.parametrize("cid,fields,expected", FIELD_CASES, ids=[c[0] for c in FIELD_CASES])
def test_field_validation(cid, fields, expected):
    assert core.validate_fields(fields, MON, AREAS, BY_ID["FAC-A"]) == expected


# ------------------------------------------------------------------ rules

D = "2026-10-12"
DECIDE_CASES = [
    # (id, label, fields, urgent, facility, service, today,
    #  template, escalate, visit, clinic_fac, radar, repoint)
    ("transport", "transport", {}, False, "FAC-A", "antenatal", MON, "transport", None, None, None, None, None),
    ("cost from table", "cost", {}, False, "FAC-A", "lab", MON, "cost", None, None, None, None, None),
    ("cost unknown -> CHW", "cost", {}, False, "FAC-B", "lab", MON, "unknown", "non_clinical", None, None, None, None),
    ("doc checklist", "missing_documents", {}, False, "FAC-A", "hiv", MON, "missing_documents", None, None, None, None, None),
    ("lost letter -> CHW", "missing_documents", {"missing_doc": "referral letter"}, False, "FAC-A", "hiv", MON,
     "missing_documents", "non_clinical", None, None, None, None),
    ("scheduling no date", "scheduling", {}, False, "FAC-A", "hiv", MON, "scheduling", None, None, None, None, None),
    ("scheduling with date", "scheduling", {"return_date": D}, False, "FAC-A", "hiv", MON,
     "scheduling_set", None, D, "FAC-A", None, None),
    ("closed but open today (D9)", "clinic_closed", {}, False, "FAC-A", "lab", MON,
     "clinic_hours", None, None, None, "FAC-A", None),
    ("closed today, alt open (D9)", "clinic_closed", {}, False, "FAC-B", "lab", SUN,
     "clinic_alternative", None, None, None, "FAC-B", None),
    ("closed today, no alt", "clinic_closed", {}, False, "FAC-C", "eye", MON,
     "unknown", "non_clinical", None, None, "FAC-C", None),
    ("closed + return date", "clinic_closed", {"return_date": D}, False, "FAC-A", "lab", MON,
     "clinic_hours", None, D, "FAC-A", "FAC-A", None),
    ("turned away no date", "turned_away", {}, False, "FAC-B", "tb", MON,
     "unknown", "non_clinical", None, None, "FAC-B", None),
    ("wrong facility reroute", "wrong_facility", {"area": "Kayole"}, False, "FAC-A", "eye", MON,
     "wrong_facility", None, None, None, None, "FAC-C"),
    ("wrong facility, none offers", "wrong_facility", {}, False, "FAC-A", "dialysis", MON,
     "unknown", "non_clinical", None, None, None, None),
    ("swahili resend", "language", {"language": "sw"}, False, "FAC-A", "hiv", MON, "language_sw", None, None, None, None, None),
    ("language not sw -> CHW", "language", {}, False, "FAC-A", "hiv", MON, "unknown", "non_clinical", None, None, None, None),
    ("fear explainer", "fear_confusion", {}, False, "FAC-A", "hiv", MON, "fear_confusion", None, None, None, None, None),
    ("clinical", "clinical_symptom", {}, True, "FAC-A", "hiv", MON, "clinical", "clinical", None, None, None, None),
    ("unknown after outage -> clinical timing (D6)", "unknown", {}, True, "FAC-A", "hiv", MON,
     "unknown", "clinical", None, None, None, None),
    ("unknown, low confidence", "unknown", {}, False, "FAC-A", "hiv", MON, "unknown", "non_clinical", None, None, None, None),
    ("plan with date", "plan_ack", {"return_date": D}, False, "FAC-A", "hiv", MON, "plan_ack", None, D, "FAC-A", None, None),
    ("plan no date", "plan_ack", {}, False, "FAC-A", "hiv", MON, "plan_ack_nodate", None, None, None, None, None),
]


@pytest.mark.parametrize(
    "cid,label,fields,urgent,fac,service,today,template,escalate,visit,clinic_fac,radar,repoint",
    DECIDE_CASES, ids=[c[0] for c in DECIDE_CASES],
)
def test_decide(cid, label, fields, urgent, fac, service, today, template, escalate, visit, clinic_fac, radar, repoint):
    case = {"id": "R-0142", "service": service, "area": BY_ID[fac]["area"]}
    cls = core.Classification(label, fields, urgent)
    p = core.decide(cls, case, BY_ID[fac], FACILITIES, today, EMERGENCY)

    assert p.patient[0] == template
    assert p.escalate == escalate
    assert p.set_visit_date == visit
    assert (p.clinic[0] if p.clinic else None) == clinic_fac
    assert p.radar_event == radar
    assert p.repoint_to == repoint
    core.render_plan(p)  # every plan renders with no missing slot


SAT = date(2026, 10, 10)

ALTERNATIVE_CASES = [
    # (id, closed facility, service, today, expected alternative name)
    # Sunday: FAC-A (lab) is closed too, so only 24 h FAC-D may be suggested.
    ("skips a closed alternative", "FAC-B", "lab", SUN, "Demo Faraja Hospital"),
    # Saturday: FAC-A opens 8-12 and is listed first; it must win over FAC-D.
    ("open alternative in list order", "FAC-B", "lab", SAT, "Demo Mto Health Centre"),
]


@pytest.mark.parametrize("cid,fac,service,today,alt_name", ALTERNATIVE_CASES, ids=[c[0] for c in ALTERNATIVE_CASES])
def test_closed_today_suggests_an_open_facility(cid, fac, service, today, alt_name):
    case = {"id": "R-0142", "service": service, "area": BY_ID[fac]["area"]}
    p = core.decide(core.Classification("clinic_closed"), case, BY_ID[fac], FACILITIES, today, EMERGENCY)
    key, slots = p.patient
    assert key == "clinic_alternative"
    assert slots["alt_name"] == alt_name
    alt = next(f for f in FACILITIES if f["name"] == alt_name)
    assert core.is_open_on(alt, today) and service in alt["services"]


@pytest.mark.parametrize("text,emergency,clinical", [
    ("I can't breathe", True, True),
    ("sometimes I think about suicide", True, True),
    ("my back hurts", False, True),           # clinical, but not an emergency
    ("I sell chestnuts", False, False),       # whole words only
])
def test_emergency_terms_are_a_subset_of_clinical(text, emergency, clinical):
    assert core.emergency_hit(text) is emergency and core.clinical_hit(text) is clinical


@pytest.mark.parametrize("text,stop", [("STOP", True), (" stop. ", True), ("Quit", True), ("stop by friday", False)])
def test_is_stop(text, stop):
    assert core.is_stop(text) is stop


def test_clinical_sms_uses_only_table_facts():
    case = {"id": "R-0142", "service": "hiv", "area": "Kayole"}
    p = core.decide(core.Classification("clinical_symptom", urgent=True), case, BY_ID["FAC-A"], FACILITIES, MON, EMERGENCY)
    sms = core.render_plan(p)
    assert EMERGENCY["name"] in sms and "call 911" in sms
