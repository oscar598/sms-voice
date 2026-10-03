"""Deterministic decision core. Pure: no I/O, no clock (callers pass `now`).

The model only labels a patient message. Everything that decides what happens
next lives here, so every rule is unit-testable (design doc ledger; D6, D9,
R5d, R6 user directions).

Unresolved by design review, deliberately NOT implemented here:
- R5b: deferred admin actions on mixed clinical messages (claim vs DONE).
- R7c: HELP keyword reply and the "reply 2 to talk" route.
- R8b: radar denominator / time window (dashboard concern).
"""

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import templates

LABELS = {
    "transport", "cost", "wrong_facility", "missing_documents", "scheduling",
    "clinic_closed", "turned_away", "language", "fear_confusion",
    "clinical_symptom", "plan_ack", "unknown",
}
ADMIN_BARRIERS = LABELS - {"clinical_symptom", "plan_ack", "unknown"}
CONFIDENCE_FLOOR = 0.7
RETURN_DATE_WINDOW_DAYS = 30
SUPPORTED_LANGUAGES = {"en", "sw"}
AREA_MATCH_RATIO = 0.9

# Whole-word, case-insensitive. False positives are acceptable: they only
# bring a human in. Locale slang/Swahili terms are part of the list.
CLINICAL_TERMS = [
    "pain", "painful", "hurt", "hurts", "hurting", "ache", "aching", "bleed",
    "bleeding", "blood", "fever", "hot body", "vomit", "vomiting", "diarrhea",
    "can't breathe", "cant breathe", "breathing", "chest", "faint", "fainted",
    "dizzy", "seizure", "swollen", "swelling", "worse", "emergency",
    "maumivu", "damu", "homa", "kizunguzungu", "kutapika",
]
_CLINICAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in CLINICAL_TERMS) + r")\b", re.IGNORECASE
)

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "yes start"}
YES_WORDS = {"yes", "y", "ndio", "ndiyo"}
NO_WORDS = {"no", "n", "hapana"}

_CASE_REF = r"(?:r-?)?0*(\d{1,6})"
_CHW_RE = re.compile(rf"^\s*(1|done|lost)(?:\s+{_CASE_REF})?\s*$", re.IGNORECASE)
_CLINIC_RE = re.compile(rf"^\s*(y|n)(?:\s+{_CASE_REF})?\s*$", re.IGNORECASE)


def case_id(num):
    return f"R-{int(num):04d}"


def clinical_hit(text):
    return _CLINICAL_RE.search(text or "") is not None


def _norm(text):
    return " ".join((text or "").lower().split()).strip(" .!?")


# ---------------------------------------------------------------- routing


@dataclass
class Route:
    kind: str  # clinical | opt_out | opt_in | ignore | yes | no | holding | classify
    #            chw_claim | chw_done | chw_lost | clinic_seen | clinic_not_seen | unparsed
    case_id: str = None


def route(body, role, case=None, open_prompt=None):
    """Decide what an inbound SMS is, before any model call.

    role: 'patient' | 'chw' | 'clinic' (from the sender's phone number).
    case: the patient's open case dict (patients only).
    open_prompt: the `expects` value of the patient's open prompt, or None.
    """
    if role == "chw":
        m = _CHW_RE.match(body or "")
        if not m:
            return Route("unparsed")
        verb = {"1": "chw_claim", "done": "chw_done", "lost": "chw_lost"}[m.group(1).lower()]
        return Route(verb, case_id(m.group(2)) if m.group(2) else None)

    if role == "clinic":
        m = _CLINIC_RE.match(body or "")
        if not m:
            return Route("unparsed")
        verb = "clinic_seen" if m.group(1).lower() == "y" else "clinic_not_seen"
        return Route(verb, case_id(m.group(2)) if m.group(2) else None)

    case = case or {}
    text = _norm(body)

    # R5d (user): after STOP, nothing goes to the patient, clinical included.
    if case.get("opted_out"):
        return Route("opt_in") if text in START_WORDS else Route("ignore")

    # Clinical keywords run before every other patient rule.
    if clinical_hit(body):
        return Route("clinical")
    if text in STOP_WORDS:
        return Route("opt_out")
    if text in START_WORDS:
        return Route("opt_in")

    # Only an exact token counts as a control reply; longer text is classified.
    if open_prompt == "yes_no":
        if text in YES_WORDS:
            return Route("yes")
        if text in NO_WORDS:
            return Route("no")

    # R6 (user): a stuck case stays stuck and gets a fixed holding reply.
    if case.get("escalation", "none") != "none" or open_prompt is None:
        return Route("holding")

    return Route("classify")


# ----------------------------------------------------------------- guards


@dataclass
class Classification:
    label: str
    fields: dict = field(default_factory=dict)
    urgent: bool = False  # clinical baton timing (5 min/CHW) + safety template
    reason: str = "ok"


def _valid_return_date(value, today):
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return None
    if today <= d <= today + timedelta(days=RETURN_DATE_WINDOW_DAYS):
        return d.isoformat()
    return None


def _valid_area(value, areas):
    if not value:
        return None
    best = max(areas, key=lambda a: difflib.SequenceMatcher(None, value.lower(), a.lower()).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, value.lower(), best.lower()).ratio() >= AREA_MATCH_RATIO:
        return best
    return None


def validate_fields(raw_fields, today, areas, facility):
    """Keep only fields that pass their check; drop the rest silently."""
    raw_fields = raw_fields if isinstance(raw_fields, dict) else {}
    out = {}
    d = _valid_return_date(raw_fields.get("return_date"), today)
    if d:
        out["return_date"] = d
    if raw_fields.get("language") in SUPPORTED_LANGUAGES:
        out["language"] = raw_fields["language"]
    a = _valid_area(raw_fields.get("area"), areas)
    if a:
        out["area"] = a
    doc = str(raw_fields.get("missing_doc", "")).lower()
    if doc and doc in [r.lower() for r in facility.get("required_docs", [])]:
        out["missing_doc"] = doc
    return out


def apply_guards(raw, today, areas, facility, error=False):
    """Turn model output (or a model failure) into a safe Classification."""
    if error or not isinstance(raw, dict):
        # D6: an outage must never slow down a possibly clinical message.
        return Classification("unknown", urgent=True, reason="model_error")
    label = raw.get("barrier")
    conf = raw.get("confidence")
    if label not in LABELS or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return Classification("unknown", urgent=True, reason="model_error")

    if raw.get("clinical_flag") is True or label == "clinical_symptom":
        return Classification("clinical_symptom", urgent=True, reason="clinical")
    if conf < CONFIDENCE_FLOOR:
        return Classification("unknown", reason="low_confidence")
    others = {m for m in raw.get("also_mentions") or [] if m in ADMIN_BARRIERS and m != label}
    if others:
        return Classification("unknown", reason="multi_barrier")

    return Classification(label, validate_fields(raw.get("fields"), today, areas, facility))


# ------------------------------------------------------------------ rules


@dataclass
class Plan:
    patient: tuple = None  # (template key, slots)
    clinic: tuple = None  # (facility id, template key, slots)
    escalate: str = None  # None | 'clinical' | 'non_clinical'
    set_visit_date: str = None
    repoint_to: str = None
    radar_event: str = None  # facility id the barrier counts against


def is_open_on(facility, day):
    return bool(facility.get("hours", {}).get(day.weekday()))


def _slots(f, prefix=""):
    return {
        f"{prefix}name": f["name"], f"{prefix}hours": f["hours_text"],
        f"{prefix}address": f["address"],
    }


def _alternative(facilities, service, exclude_id, area=None, open_on=None):
    """First facility offering `service`, same area preferred."""
    pool = [f for f in facilities if f["id"] != exclude_id and service in f["services"]]
    if open_on:
        pool = [f for f in pool if is_open_on(f, open_on)]
    pool.sort(key=lambda f: f["area"] != area)
    return pool[0] if pool else None


def _visit(plan, case, facility, d):
    plan.set_visit_date = d
    plan.clinic = (facility["id"], "arriving", {"case_id": case["id"], "date": d})


def decide(cls, case, facility, facilities, today, emergency):
    """Map a guarded label to exactly one approved workflow."""
    label, fl = cls.label, cls.fields
    p = Plan()
    base = {**_slots(facility), "service": case["service"]}

    def to_human(clinical=False):
        p.patient = ("unknown", {})
        p.escalate = "clinical" if clinical else "non_clinical"
        return p

    if label == "clinical_symptom":
        p.patient = ("clinical", {"emergency_name": emergency["name"], "emergency_number": emergency["number"]})
        p.escalate = "clinical"
    elif label == "unknown":
        to_human(clinical=cls.urgent)
    elif label == "transport":
        p.patient = ("transport", {**base, "transport_note": facility["transport_note"]})
    elif label == "cost":
        if not facility.get("cost_note"):
            return to_human()
        p.patient = ("cost", {"cost_note": facility["cost_note"]})
    elif label == "missing_documents":
        p.patient = ("missing_documents", {**base, "required_docs": ", ".join(facility["required_docs"])})
        if fl.get("missing_doc") == "referral letter":
            p.escalate = "non_clinical"  # only a CHW can reissue the letter
    elif label == "scheduling":
        if "return_date" in fl:
            p.patient = ("scheduling_set", {**base, "date": fl["return_date"]})
            _visit(p, case, facility, fl["return_date"])
        else:
            p.patient = ("scheduling", {**base, "phone": facility["phone"]})
    elif label == "clinic_closed":
        p.radar_event = facility["id"]
        # D9: posted hours only; an alternative only when closed today.
        if is_open_on(facility, today):
            p.patient = ("clinic_hours", base)
        else:
            alt = _alternative(facilities, case["service"], facility["id"], facility["area"], open_on=today)
            if not alt:
                return to_human()
            p.patient = ("clinic_alternative", {**base, **_slots(alt, "alt_")})
        if "return_date" in fl:
            _visit(p, case, facility, fl["return_date"])
    elif label == "turned_away":
        p.radar_event = facility["id"]
        if "return_date" not in fl:
            return to_human()
        p.patient = ("turned_away", {**base, "date": fl["return_date"]})
        _visit(p, case, facility, fl["return_date"])
    elif label == "wrong_facility":
        alt = _alternative(facilities, case["service"], facility["id"], fl.get("area") or case.get("area"))
        if not alt:
            return to_human()
        p.patient = ("wrong_facility", _slots(alt, "alt_"))
        p.repoint_to = alt["id"]
    elif label == "language":
        if fl.get("language") != "sw":
            return to_human()
        p.patient = ("language_sw", base)
    elif label == "fear_confusion":
        p.patient = ("fear_confusion", base)
    elif label == "plan_ack":
        if "return_date" in fl:
            p.patient = ("plan_ack", {**base, "date": fl["return_date"]})
            _visit(p, case, facility, fl["return_date"])
        else:
            p.patient = ("plan_ack_nodate", {})
    return p


def render_plan(plan):
    """Render the patient template so tests and callers see the exact SMS."""
    key, slots = plan.patient
    return templates.render(templates.PATIENT, key, **slots)
