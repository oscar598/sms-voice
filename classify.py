"""Claude calls. The only place the AI is used.

classify()  labels one patient SMS for the referral agent (app.py, eval.py).
chat_json() runs one multi-turn conversation step for server.py.

Both return schema-constrained JSON, and callers still validate every value
(core.apply_guards() / server.ask_claude()), so nothing here is trusted on its
own. Any failure (timeout, API error, refusal, truncated output) raises
ClassifierUnavailable, which callers turn into a hand-off to a person - never
a guessed answer.

    python classify.py "went there yesterday the gate was locked"
    python classify.py            # interactive: type messages, empty line to quit
"""

import json
import os
from datetime import date

import anthropic

import core
import seed

FALLBACK_BETA = "server-side-fallback-2026-07-01"
BARRIERS = list(core.LABELS)
# Every document any facility asks for; Claude may only name one of these.
DOCS = sorted({d for f in seed.FACILITIES for d in f["required_docs"]})


def model():
    """Read when called, so a CLASSIFY_MODEL set in .env is used even if .env loads late."""
    return os.environ.get("CLASSIFY_MODEL", "claude-opus-5-5")


def timeout_s():
    # Live calls measured 2.5-3.5 s (2026-10-03), so the original 3 s budget timed
    # out about half the time. 6 s keeps most SMS replies near the 5 s goal.
    return float(os.environ.get("CLASSIFY_TIMEOUT", "6.0"))


SYSTEM = """You label one SMS from a patient who was referred to a clinic in Nairobi, Kenya. \
Messages may be English, Swahili, Sheng, slang or misspelled. You only label; you never reply to the patient.

Pick the single best `barrier`:
- transport: can't get there (fare, distance, no matatu, no one to take them)
- cost: worried about fees, insurance (NHIF/SHA), lab or drug costs
- wrong_facility: the facility doesn't offer the service, or the wrong place
- missing_documents: lacks referral letter, ID, insurance card, clinic book or lab results
- scheduling: can't get or doesn't know the appointment; timing clashes with work
- clinic_closed: went and the facility was shut
- turned_away: facility was open but sent them home (queue full, "come back Monday"), \
even if they were given or have chosen a new date - put that date in `return_date`
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


def _request(system, messages, schema, max_tokens):
    return dict(
        model=model(),
        max_tokens=max_tokens,  # thinking is always on for this model; leave room for it + the JSON
        betas=[FALLBACK_BETA],
        fallbacks="default",  # a safety decline re-runs on Anthropic's recommended fallback model
        system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=messages,
    )


def build_request(text, today):
    return _request(SYSTEM, [{"role": "user", "content": f"Today is {today.isoformat()} ({today:%A}).\nSMS: {text}"}],
                    SCHEMA, max_tokens=2048)


def _send(request, client=None, timeout=None, meta=None):
    """Make the call and return the parsed JSON, or raise ClassifierUnavailable."""
    try:
        client = client or _default_client()
        if timeout is not None:
            client = client.with_options(timeout=timeout)
        response = client.beta.messages.create(**request)
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
        return json.loads(text_block.text)
    except json.JSONDecodeError as e:
        raise ClassifierUnavailable("invalid json") from e


def classify(text, today=None, client=None, meta=None):
    """Return the model's label dict, or raise ClassifierUnavailable.

    If `meta` is a dict, token usage is recorded in it (used by eval.py).
    """
    out = _send(build_request(text, today or date.today()), client=client, meta=meta)
    # Nulls mean "not stated"; core.validate_fields() expects them absent.
    out["fields"] = {k: v for k, v in (out.get("fields") or {}).items() if v is not None}
    return out


def chat_json(system, messages, schema, timeout=None, client=None, max_tokens=4096):
    """One multi-turn Claude call that must return JSON matching `schema` (server.py).

    `messages` is [{"role": "user"|"assistant", "content": str}, ...] ending with
    a user turn.
    """
    turns = [{"role": m["role"], "content": m["content"]} for m in messages]
    if turns and turns[0]["role"] != "user":
        # The API requires a user turn first; ours often open with the clinic's SMS.
        turns.insert(0, {"role": "user", "content": "(The clinic starts the conversation.)"})
    return _send(_request(system, turns, schema, max_tokens), client=client, timeout=timeout)


# ------------------------------------------------------------- command line
# Runs one message through eval.simulate() - the same keyword filter, guards and
# rules as a live SMS - for the demo case R-0142. Nothing is saved and no SMS is
# sent. Claude is called even when a clinical keyword matches, to show its label.


def _cli(argv):
    import sys

    import envfile
    import eval as ev

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    envfile.load()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env or your shell.")

    today = date.today()
    case = dict(seed.DEMO_CASE, area=seed.DEMO_CASE["patient_area"])
    facility = seed.FACILITY_BY_ID[case["facility_id"]]
    print(f"Model {model()}, timeout {timeout_s():g}s, today {today:%a %Y-%m-%d}")

    def run(text):
        s = ev.simulate(text, case, facility, today, ev.classifier_fn(), always_call_model=True)
        print(f"\nMessage:   {text}")
        if s.error:
            print(f"Claude:    FAILED ({s.error}) after {s.latency_s:.1f}s")
        elif s.raw is not None:
            print(f"Claude:    {json.dumps(s.raw, ensure_ascii=False)}  ({s.latency_s:.1f}s)")
        if s.source == "keyword":
            print("Keyword:   clinical keyword found - the live app skips Claude for this message")
        print(f"Decision:  {s.label or s.route}  (reason: {s.reason})")
        print(f"Patient:   {s.patient_sms or '(no SMS)'}")
        if s.help_sms:
            print(f"Patient:   {s.help_sms}")
        if s.clinic_sms:
            print(f"Clinic:    {s.clinic_sms}")
        if s.escalate:
            print(f"CHW:       case handed to a health worker ({s.escalate})")

    if argv:
        run(" ".join(argv))
        return
    while True:
        try:
            text = input("\npatient> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            break
        run(text)


if __name__ == "__main__":
    import sys

    _cli(sys.argv[1:])
