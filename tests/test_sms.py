"""The one SMS length rule (sms.py) and the fixed SMS that rely on it."""

from datetime import date

import pytest

import sms


def test_default_limit_is_one_segment(monkeypatch):
    monkeypatch.delenv("SMS_MAX_CHARS", raising=False)
    assert sms.limit() == 160
    monkeypatch.setenv("SMS_MAX_CHARS", "70")
    assert sms.limit() == 70


@pytest.mark.parametrize("text,expected", [
    ("Thanks! See you Monday.", "Thanks! See you Monday."),
    ("it’s “fine” – really…", "it's \"fine\" - really..."),     # curly quotes, dash, ellipsis
    ("see you 👍 soon", "see you soon"),                          # emoji dropped, no double space
    ("français", "francais"),                                     # ç has no lowercase GSM-7 form
    ("line one\nline two", "line one\nline two"),
])
def test_to_gsm(text, expected):
    assert sms.to_gsm(text) == expected
    assert set(sms.to_gsm(text)) <= sms.GSM7


def test_fit_leaves_short_text_alone():
    assert sms.fit("Need help getting here? Call us.") == "Need help getting here? Call us."


def test_fit_cuts_at_a_word_and_marks_it():
    text = " ".join(["word"] * 60)  # 299 chars
    out = sms.fit(text)
    assert len(out) <= 160 and out.endswith("...") and not out[:-3].endswith(" ")
    assert out[:-3].split() == ["word"] * len(out[:-3].split())  # no word cut in half


def test_fit_respects_the_setting(monkeypatch):
    monkeypatch.setenv("SMS_MAX_CHARS", "50")
    assert len(sms.fit("x " * 100)) <= 50


def test_server_fixed_replies_fit():
    import server
    long_name = {"name": "Bartholomew-Alexander Okonkwo-Smith", "appointment_datetime": "2026-10-07 13:30"}
    for text in (server.EMERGENCY_REPLY, server.HANDOFF_REPLY, server.opening_message(long_name)):
        assert len(text) <= sms.limit() and set(text) <= sms.GSM7, text
    assert "call 911" in server.EMERGENCY_REPLY


def test_relay_quotes_get_the_room_the_limit_leaves():
    import app
    import templates
    long = "nimekuwa nikisubiri hapa kwa muda mrefu sana " * 6
    for make in (lambda q: templates.CHW_RELAY + q,
                 lambda q: templates.render(templates.CHW, "from_patient", case_id="R-0142", quote=q)):
        out = app._with_quote(make, long)
        assert len(out) <= sms.limit() and out.endswith(("...", '..."'))


def test_every_barrier_answer_is_followed_by_the_same_help_sms():
    import core
    import seed
    import templates
    fac = seed.FACILITY_BY_ID["FAC-B"]
    for label in core.LABELS:
        cls = core.Classification(label, {}, urgent=label == "clinical_symptom")
        plan = core.decide(cls, dict(seed.DEMO_CASE), fac, seed.FACILITIES, date(2026, 10, 5), seed.EMERGENCY)
        text = core.help_sms(plan)
        if plan.reask:
            assert text is None, label  # a question back, not an answer
            continue
        phone = seed.FACILITY_BY_ID[plan.repoint_to]["phone"] if plan.repoint_to else fac["phone"]
        assert text == templates.HELP_SMS.format(phone=phone), label
        assert "another question" in text and len(text) <= sms.limit() and set(text) <= sms.GSM7


def test_simulated_replies_never_exceed_the_limit():
    import eval as ev
    import seed
    long_fac = dict(seed.FACILITY_BY_ID["FAC-B"],
                    name="NewYork-Presbyterian/Columbia University Irving Medical Center",
                    address="622 West 168th Street, New York, NY 10032")
    fake = lambda text, today, meta: {"barrier": "transport", "confidence": 0.9,  # noqa: E731
                                      "clinical_flag": False, "also_mentions": [], "fields": {}}
    s = ev.simulate("no way to get there", dict(seed.DEMO_CASE, area="Embakasi"), long_fac,
                    date(2026, 10, 5), fake)
    assert len(s.patient_sms) <= sms.limit()
