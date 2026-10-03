"""Evaluate the classifier on labeled patient SMS (T7, D11).

Runs each message through the real decision path - clinical keyword filter,
then Claude, then guards, then the rules table - and compares the resulting
label and action with the gold label.

    .venv/bin/python eval.py tuning            # the 100 messages we tune on
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
import seed

EVAL_DIR = Path(__file__).parent / "eval"
TODAY = date(2026, 10, 5)  # fixed so gold return dates stay correct
CASE = {"id": "R-EVAL", "service": "lab", "area": "Embakasi", "facility_id": "FAC-B"}
CONFIDENCE_FLOOR = core.CONFIDENCE_FLOOR
HELDOUT_MIN_CLINICAL = 15
EVAL_TIMEOUT_S = 30.0  # long enough to measure real latency; compared to the 3 s budget below


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
    for n, line in enumerate(path.read_text().splitlines(), 1):
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


def run_one(row, classify_fn):
    facility = seed.FACILITY_BY_ID[CASE["facility_id"]]
    gold_cls = core.Classification(row["label"], row.get("fields", {}), urgent=row["label"] == "clinical_symptom")
    gold_action = action(gold_cls)

    route = core.route(row["text"], "patient", {"escalation": "none"}, "free_text")
    if route.kind == "clinical":
        cls = core.Classification("clinical_symptom", urgent=True, reason="keyword")
        return Result(row["text"], row["label"], cls.label, "keyword", gold_action, action(cls))

    meta, start = {}, time.monotonic()
    try:
        raw, error = classify_fn(row["text"], TODAY, meta), False
    except classify.ClassifierUnavailable as e:
        raw, error = None, True
        meta["error"] = str(e)
    latency = time.monotonic() - start
    cls = core.apply_guards(raw, TODAY, seed.AREAS, facility, error=error)
    return Result(
        row["text"], row["label"], cls.label, "model_error" if error else "model", gold_action, action(cls),
        raw_label=(raw or {}).get("barrier"), confidence=(raw or {}).get("confidence"),
        reason=meta.get("error") or cls.reason, latency_s=round(latency, 3), usage=meta.get("usage", {}),
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


def live_classifier():
    import anthropic

    client = anthropic.Anthropic(timeout=EVAL_TIMEOUT_S, max_retries=0)

    def fn(text, today, meta):
        return classify.classify(text, today, client=client, meta=meta)
    return fn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set", choices=["tuning", "heldout"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    rows = load(args.set)[: args.limit]
    fn = live_classifier()
    results = []
    for i, row in enumerate(rows, 1):
        results.append(run_one(row, fn))
        print(f"\r{i}/{len(rows)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    out = EVAL_DIR / f"results-{args.set}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    with out.open("w") as f:
        for r in results:
            f.write(json.dumps({**asdict(r), "wrong_action": r.wrong_action}, ensure_ascii=False) + "\n")
    print_report(args.set, summarize(results, classify.TIMEOUT_S))
    print(f"\n  per-message results: {out}")


if __name__ == "__main__":
    main()
