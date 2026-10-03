"""Model call that labels a patient message. The only place the AI is used.

The model returns schema-constrained JSON; core.apply_guards() still validates
every value, so nothing here is trusted on its own. Any failure (timeout,
API error, refusal, truncated output) raises ClassifierUnavailable, which the
caller turns into an urgent CHW handoff (D6) - never a guessed label.
"""

import json
import os
from datetime import date

import anthropic

MODEL = os.environ.get("CLASSIFY_MODEL", "claude-opus-5-5")
# Live calls measured 2.5-3.5 s (2026-10-03), so the original 3 s budget timed
# out about half the time. 6 s keeps most SMS replies near the 5 s goal.


def timeout_s():
    return float(os.environ.get("CLASSIFY_TIMEOUT", "6.0"))
FALLBACK_BETA = "server-side-fallback-2026-07-01"

BARRIERS = [
    "transport", "cost", "wrong_facility", "missing_documents", "scheduling",
    "clinic_closed", "turned_away", "language", "fear_confusion",
    "clinical_symptom", "plan_ack", "unknown",
]
DOCS = ["referral letter", "national id", "clinic book", "insurance card",
        "lab results", "previous prescription"]

SYSTEM = """You label one SMS from a patient who was referred to a clinic in Nairobi, Kenya. \
Messages may be English, Swahili, Sheng, slang or misspelled. You only label; you never reply to the patient.

Pick the single best `barrier`:
- transport: can't get there (fare, distance, no matatu, no one to take them)
- cost: worried about fees, insurance (NHIF/SHA), lab or drug costs
- wrong_facility: the facility doesn't offer the service, or the wrong place
- missing_documents: lacks referral letter, ID, insurance card, clinic book or lab results
- scheduling: can't get or doesn't know the appointment; timing clashes with work
- clinic_closed: went and the facility was shut
- turned_away: facility was open but sent them home (queue full, "come back Monday")
- language: didn't understand messages or staff because of language
- fear_confusion: scared, confused about why they were referred, stigma
- clinical_symptom: mentions any symptom, pain, bleeding, fever, or feeling worse
- plan_ack: no barrier - an acknowledgement or a stated plan ("ok thanks", "I'll go Monday")
- unknown: unclear, off-topic, or you are not sure

Rules:
- Set `clinical_flag` true whenever the message mentions any symptom or health complaint, \
even alongside another barrier. When in doubt about a symptom, set it true.
- If the message names more than one barrier, put the main one in `barrier` and every other \
barrier in `also_mentions`. Otherwise `also_mentions` is empty.
- `confidence` is 0 to 1. Use a low value when the message is ambiguous.
- `fields`: use null for anything not stated. `return_date` is an ISO date (YYYY-MM-DD) the \
patient says they will go or were told to return, resolved against the date given with the message. \
`language` is "sw" if they ask for Swahili, "en" if they ask for English. `area` is the \
neighbourhood they mention. `missing_doc` is the document they say they lack."""

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "barrier": {"type": "string", "enum": BARRIERS},
        "confidence": {"type": "number"},
        "clinical_flag": {"type": "boolean"},
        "also_mentions": {"type": "array", "items": {"type": "string", "enum": BARRIERS}},
        "fields": {
            "type": "object",
            "properties": {
                "return_date": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
                "language": {"anyOf": [{"type": "string", "enum": ["en", "sw"]}, {"type": "null"}]},
                "area": _NULLABLE_STR,
                "missing_doc": {"anyOf": [{"type": "string", "enum": DOCS}, {"type": "null"}]},
            },
            "required": ["return_date", "language", "area", "missing_doc"],
            "additionalProperties": False,
        },
    },
    "required": ["barrier", "confidence", "clinical_flag", "also_mentions", "fields"],
    "additionalProperties": False,
}


class ClassifierUnavailable(Exception):
    pass


_client = None


def _default_client():
    global _client
    if _client is None:
        # No SDK retries: each retry would add another full timeout to the SMS reply.
        _client = anthropic.Anthropic(timeout=timeout_s(), max_retries=0)
    return _client


def build_request(text, today):
    return dict(
        model=MODEL,
        max_tokens=2048,  # thinking is always on for this model; leave room for it + ~100 tokens of JSON
        betas=[FALLBACK_BETA],
        fallbacks="default",  # a safety decline re-runs on Anthropic's recommended fallback model
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Today is {today.isoformat()} ({today:%A}).\nSMS: {text}"}],
    )


def classify(text, today=None, client=None, meta=None):
    """Return the model's label dict, or raise ClassifierUnavailable.

    If `meta` is a dict, token usage is recorded in it (used by eval.py).
    """
    today = today or date.today()
    try:
        client = client or _default_client()
        response = client.beta.messages.create(**build_request(text, today))
    except anthropic.AnthropicError as e:  # timeout, connection, 4xx/5xx
        raise ClassifierUnavailable(type(e).__name__) from e
    except TypeError as e:
        # The SDK reports missing credentials as a TypeError at request time.
        if "authentication" not in str(e):
            raise
        raise ClassifierUnavailable("no credentials") from e

    usage = getattr(response, "usage", None)
    if meta is not None and usage is not None:
        meta["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    if response.stop_reason != "end_turn":  # refusal (whole chain declined) or max_tokens
        raise ClassifierUnavailable(f"stop_reason={response.stop_reason}")
    text_block = next((b for b in response.content if b.type == "text"), None)
    if text_block is None:
        raise ClassifierUnavailable("no text block")
    try:
        out = json.loads(text_block.text)
    except json.JSONDecodeError as e:
        raise ClassifierUnavailable("invalid json") from e

    # Nulls mean "not stated"; core.validate_fields() expects them absent.
    out["fields"] = {k: v for k, v in (out.get("fields") or {}).items() if v is not None}
    return out
