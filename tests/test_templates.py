"""SMS length budget, rendered with the real seed data.

Every SMS: at most SMS_MAX_CHARS (sms.limit(), 160 by default). Patient SMS are
held to 120 so they still fit one segment under the Twilio trial prefix.
A single non-GSM-7 character switches the whole SMS to UCS-2 (70 chars per
segment), so every template must render in the GSM-7 basic alphabet.
"""

import itertools

import pytest

import seed
import sms
import templates
from app import QUOTE_MAX, _quote

GSM7 = sms.GSM7
PATIENT_MAX = min(120, sms.limit())
CLINIC_MAX = CHW_MAX = sms.limit()
DATE = "2026-10-12"


def patient_slots():
    """Every facility (and every other facility as the alternative), every service."""
    for f, alt in itertools.product(seed.FACILITIES, seed.FACILITIES):
        if alt is f:  # core._alternative never offers the same facility
            continue
        for service in f["services"]:
            yield {
                "name": f["name"], "hours": f["hours_text"], "address": f["address"],
                "phone": f["phone"], "service": service, "date": DATE,
                "transport_note": f["transport_note"], "cost_note": f["cost_note"],
                "required_docs": ", ".join(f["required_docs"]),
                "alt_name": alt["name"], "alt_hours": alt["hours_text"], "alt_address": alt["address"],
                "emergency_name": seed.EMERGENCY["name"],
            }


def worst(table, key, slot_sets):
    rendered = [templates.render(table, key, **s) for s in slot_sets]
    return max(rendered, key=len)


@pytest.mark.parametrize("key", sorted(templates.PATIENT))
def test_patient_template_fits_one_segment(key):
    sms = worst(templates.PATIENT, key, list(patient_slots()))
    assert len(sms) <= PATIENT_MAX, f"{len(sms)} chars: {sms}"
    assert set(sms) <= GSM7, f"non-GSM-7: {set(sms) - GSM7}"


@pytest.mark.parametrize("key", sorted(templates.CLINIC))
def test_clinic_template_fits_one_segment(key):
    sms = templates.render(templates.CLINIC, key, case_id="R-9999", date=DATE)
    assert len(sms) <= CLINIC_MAX and set(sms) <= GSM7


LONG_QUOTE = "naskia kizunguzungu sana na damu inatoka tangu jana usiku, siwezi kutembea vizuri hata kidogo"


@pytest.mark.parametrize("key", sorted(templates.CHW))
def test_chw_template_fits_two_segments(key):
    sms = templates.render(templates.CHW, key, kind="NON-CLINICAL", case_id="R-9999",
                           phone="+254700000301", quote=_quote(LONG_QUOTE))
    assert len(sms) <= CHW_MAX and set(sms) <= GSM7, sms


@pytest.mark.parametrize("text,expected_len", [
    ("short", 5),
    ("x" * QUOTE_MAX, QUOTE_MAX),
    ("x" * (QUOTE_MAX + 1), QUOTE_MAX),
    ("  spaced \n out  ", len("spaced out")),
])
def test_quote_truncation(text, expected_len):
    q = _quote(text)
    assert len(q) == expected_len
    if len(text.strip()) > QUOTE_MAX:
        assert q.endswith("...")
