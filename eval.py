"""Evaluate the classifier on labeled patient SMS (T7, D11).

Runs each message through the real decision path - clinical keyword filter,
then Claude, then guards, then the rules table - and compares the resulting
label and action with the gold label.

    .venv/bin/python eval.py tuning            # the messages we tune on (also the scenario report's input)
    .venv/bin/python eval.py heldout           # the 50 a teammate wrote (headline numbers)
    .venv/bin/python eval.py tuning --limit 10

Headline numbers come from the held-out set only (D11): 100% recall on its
clinical messages, wrong actions reported as k/N (target <= 1).
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import classify
import core
import envfile
import seed
import sms
import templates

EVAL_DIR = Path(__file__).parent / "eval"
TODAY = date(2026, 10, 5)  # fixed so gold return dates stay correct
CASE = {"id": "R-EVAL", "service": "lab", "area": "Embakasi", "facility_id": "FAC-B"}
CONFIDENCE_FLOOR = core.CONFIDENCE_FLOOR
HELDOUT_MIN_CLINICAL = 15
EVAL_TIMEOUT_S = 30.0  # long enough to measure real latency; compared to classify.timeout_s() below


@dataclass
class Result:
    text: str
    gold: str
    pred: str
    source: str  # keyword | model | model_error
    gold_action: tuple
    pred_action: tuple
    raw_label: str = None
    confidence: float = None
    reason: str = None
    latency_s: float = None
    usage: dict = field(default_factory=dict)

    @property
    def wrong_action(self):
        # Wrong = an automated workflow that differs from gold. Handing off to a
        # human (any escalation) is never counted as wrong.
        return self.pred_action != self.gold_action and self.pred_action[1] is None


def load(name):
    path = EVAL_DIR / f"{name}.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found. The held-out set is written by a teammate who does not tune "
                 f"the prompt (D11); see eval/heldout.TEMPLATE.jsonl for the format.")
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.exit(f"{path.name} line {n} is not valid JSON ({e.msg}): {line[:80]}")
    bad = [r for r in rows if r["label"] not in core.LABELS]
    if bad:
        sys.exit(f"unknown labels in {path.name}: {sorted({r['label'] for r in bad})}")
    if name == "heldout":
        n = sum(r["label"] == "clinical_symptom" for r in rows)
        if n < HELDOUT_MIN_CLINICAL:
            sys.exit(f"heldout has {n} clinical messages; D11 requires at least {HELDOUT_MIN_CLINICAL}")
    return rows


def action(cls):
    facility = seed.FACILITY_BY_ID[CASE["facility_id"]]
    plan = core.decide(cls, CASE, facility, seed.FACILITIES, TODAY, seed.EMERGENCY)
    return (plan.patient[0], plan.escalate)


# ------------------------------------------------------------- one message, offline


@dataclass
class Simulation:
    """Everything that happens to one patient SMS, without a database or a phone."""
    route: str                  # core.route() kind: classify | clinical | opt_out | ask_date | ...
    source: str                 # model | model_error | keyword | rule
    label: str = None           # final label after guards (None for rule replies)
    reason: str = None          # why: ok | low_confidence | multi_barrier | keyword | rule text ...
    raw: dict = None            # Claude's JSON, when Claude was called
    error: str = None           # why Claude failed, when it did
    template: str = None        # patient template key
    patient_sms: str = None
    help_sms: str = None        # second patient SMS after a barrier reply (core.help_sms)
    clinic_sms: str = None
    escalate: str = None        # None | clinical | non_clinical
    latency_s: float = None
    usage: dict = field(default_factory=dict)


RULE_REPLIES = {  # route kinds answered by code without Claude, and what they mean
    "opt_out": "STOP: opted out, nothing more is sent",
    "opt_in": "START: messages resume",
    "holding": "case is waiting for a person: fixed holding reply",
    "yes": "exact YES to a yes/no question",
    "no": "exact NO to a yes/no question",
    "ask_barrier": "bare reply: asks what is stopping them",
    "ask_date": "bare reply: asks which day they will go",
    "ask_again": "bare reply: asks again",
    "ack_visit": "bare reply: confirms the visit date",
}


def simulate(text, case, facility, today, classify_fn, facilities=None, emergency=None,
             open_prompt="free_text", use_rules=True, always_call_model=False,
             may_reask=True, directions_fn=None):
    """Run one patient SMS through the live decision path and return what happens.

    Same order as app.handle_inbound: clinical keywords, then the exact-token
    rules (STOP, bare yes/ok...), then Claude, guards and the rules table.
    classify_fn(text, today, meta) is classify.classify or a test stand-in.
    use_rules=False sends everything except keyword hits to Claude (eval.py
    measures the model). always_call_model=True also asks Claude on keyword hits,
    to show its own label. directions_fn() returns a directions SMS that replaces
    the transport template (None keeps the template).
    """
    facilities = facilities or seed.FACILITIES
    emergency = emergency or seed.EMERGENCY
    r = core.route(text, "patient", {"escalation": "none", **case}, open_prompt)
    kind = r.kind if use_rules or r.kind == "clinical" else "classify"

    if kind not in ("classify", "clinical"):
        s = Simulation(kind, "rule", reason=RULE_REPLIES.get(kind, kind))
        if kind in core.REASKS or kind == "holding":
            s.template = kind
            s.patient_sms = templates.render(templates.PATIENT, kind, name=facility["name"])
        elif kind == "ack_visit":
            s.template = "plan_ack"
            s.patient_sms = templates.render(templates.PATIENT, "plan_ack", name=facility["name"],
                                             date=case.get("visit_date"))
        elif kind == "no":
            s.template, s.patient_sms = "what_happened", templates.PATIENT["what_happened"]
        s.patient_sms = s.patient_sms and sms.fit(s.patient_sms)  # exactly what would be sent
        return s

    s = Simulation(kind, "keyword" if kind == "clinical" else "model")
    if kind == "classify" or always_call_model:
        meta, start = {}, time.monotonic()
        try:
            s.raw = classify_fn(text, today, meta)
        except classify.ClassifierUnavailable as e:
            s.error = str(e)
        s.latency_s, s.usage = round(time.monotonic() - start, 3), meta.get("usage", {})
    if kind == "clinical":
        cls = core.Classification("clinical_symptom", urgent=True, reason="keyword")
    else:
        areas = sorted({f["area"] for f in facilities})
        cls = core.apply_guards(s.raw, today, areas, facility, error=s.error is not None)
        if s.error:
            s.source = "model_error"
    plan = core.decide(cls, case, facility, facilities, today, emergency, may_reask=may_reask)
    s.label, s.reason = cls.label, (s.error if kind == "classify" and s.error else cls.reason)
    s.template, s.escalate = plan.patient[0], plan.escalate
    s.patient_sms = core.render_plan(plan)
    if s.template == "transport" and directions_fn:
        s.patient_sms = directions_fn() or s.patient_sms
    s.patient_sms = sms.fit(s.patient_sms)  # exactly what would be sent
    s.help_sms = core.help_sms(plan) and sms.fit(core.help_sms(plan))
    if plan.clinic:
        _, key, slots = plan.clinic
        s.clinic_sms = sms.fit(templates.render(templates.CLINIC, key, **slots))
    return s


def run_one(row, classify_fn):
    facility = seed.FACILITY_BY_ID[CASE["facility_id"]]
    gold_cls = core.Classification(row["label"], row.get("fields", {}), urgent=row["label"] == "clinical_symptom")
    s = simulate(row["text"], CASE, facility, TODAY, classify_fn, use_rules=False, may_reask=False)
    return Result(
        row["text"], row["label"], s.label, s.source, action(gold_cls), (s.template, s.escalate),
        raw_label=(s.raw or {}).get("barrier"), confidence=(s.raw or {}).get("confidence"),
        reason=s.reason, latency_s=s.latency_s, usage=s.usage,
    )


def summarize(results, budget_s):
    n = len(results)
    clinical = [r for r in results if r.gold == "clinical_symptom"]
    model = [r for r in results if r.source == "model"]
    wrong = [r for r in results if r.wrong_action]
    missed = [r for r in clinical if r.pred != "clinical_symptom"]
    safe_unknown = [r for r in results if r.pred == "unknown" and r.gold != "unknown"]
    errors = [r for r in model if r.raw_label != r.gold]
    lat = sorted(r.latency_s for r in model)  # failed calls would skew latency toward 0
    tokens_in = sum(r.usage.get("input_tokens", 0) for r in results)
    tokens_out = sum(r.usage.get("output_tokens", 0) for r in results)
    return {
        "messages": n,
        "clinical_recall": f"{len(clinical) - len(missed)}/{len(clinical)}",
        "wrong_actions": f"{len(wrong)}/{n}",
        # An outage also yields "unknown"; it must never count as a correct label.
        "label_accuracy": f"{sum(r.pred == r.gold and r.source != 'model_error' for r in results)}/{n}",
        "safe_unknown": f"{len(safe_unknown)}/{n}",
        "keyword_hits": sum(r.source == "keyword" for r in results),
        "model_errors": sum(r.source == "model_error" for r in results),
        "raw_errors_conf_ge_floor": sum((r.confidence or 0) >= CONFIDENCE_FLOOR for r in errors),
        "raw_errors_conf_lt_floor": sum((r.confidence or 0) < CONFIDENCE_FLOOR for r in errors),
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[max(0, int(len(lat) * 0.95) - 1)], 2) if lat else None,
        "over_budget": f"{sum(x > budget_s for x in lat)}/{len(lat)} over {budget_s}s",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "_failed_reasons": sorted({r.reason for r in results if r.source == "model_error"}),
        "_missed": missed,
        "_wrong": wrong,
    }


def print_report(name, s):
    if s["model_errors"] and s["model_errors"] + s["keyword_hits"] == s["messages"]:
        print(f"\n!! Every model call failed ({', '.join(s['_failed_reasons'])}). "
              f"These numbers do not measure the classifier. Check API credentials.")
    print(f"\n== {name}: {s['messages']} messages ==")
    for k, v in s.items():
        if not k.startswith("_"):
            print(f"  {k:26} {v}")
    for title, rows in (("Missed clinical", s["_missed"]), ("Wrong actions", s["_wrong"])):
        if rows:
            print(f"\n  {title}:")
            for r in rows:
                print(f"    [{r.gold} -> {r.pred}, conf={r.confidence}] {r.text}")


def classifier_fn(timeout=EVAL_TIMEOUT_S):
    """classify.classify with a longer timeout, for offline tools (eval, CLI, scenarios)."""
    import anthropic

    client = anthropic.Anthropic(timeout=timeout, max_retries=0)

    def fn(text, today, meta):
        return classify.classify(text, today, client=client, meta=meta)
    return fn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set", choices=["tuning", "heldout"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    envfile.load()  # ANTHROPIC_API_KEY etc. from .env, unless already set

    rows = load(args.set)[: args.limit]
    fn = classifier_fn()
    results = []
    for i, row in enumerate(rows, 1):
        results.append(run_one(row, fn))
        print(f"\r{i}/{len(rows)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    out = EVAL_DIR / f"results-{args.set}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({**asdict(r), "wrong_action": r.wrong_action}, ensure_ascii=False) + "\n")
    print_report(args.set, summarize(results, classify.timeout_s()))
    print(f"\n  per-message results: {out}")


if __name__ == "__main__":
    main()
