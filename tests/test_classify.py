"""classify.py unit tests, plus a scenario report generator.

The scenarios are the labelled messages in eval/tuning.jsonl (the set eval.py
scores), each given a patient, a real hospital and a public place as location.

    pytest                                  # unit tests only (no API calls)
    python tests/test_classify.py           # every tuning message through Claude -> eval/reports/*.txt
    python tests/test_classify.py --label scheduling
    python tests/test_classify.py --label missing_documents --limit 5 --workers 4 --no-directions
"""

import json
import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # `python tests/test_classify.py` from anywhere

import anthropic  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402

import classify  # noqa: E402
import core  # noqa: E402
import eval as ev  # noqa: E402
from scenario_context import DEFAULT_CONTEXT, EMERGENCY, HOSPITALS, NAMES, PLACES  # noqa: E402

TODAY = date(2026, 10, 5)
GOOD = {"barrier": "plan_ack", "confidence": 0.92, "clinical_flag": False, "also_mentions": [],
        "fields": {"return_date": "2026-10-12", "language": None, "area": None, "missing_doc": None}}


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response, self._exc = response, exc
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return self._response


def reply(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


def test_returns_label_and_drops_null_fields():
    out = classify.classify("ok i can go monday", TODAY, client=FakeClient(reply(GOOD)))
    assert out["barrier"] == "plan_ack"
    assert out["fields"] == {"return_date": "2026-10-12"}


def test_request_shape():
    fake = FakeClient(reply(GOOD))
    classify.classify("ok i can go monday", TODAY, client=fake)
    req = fake.calls[0]
    assert req["model"] == classify.model()
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["schema"]["properties"]["barrier"]["enum"] == classify.BARRIERS
    assert "2026-10-05 (Monday)" in req["messages"][0]["content"]  # relative dates resolve


def test_schema_enum_is_core_labels():
    assert classify.BARRIERS == list(core.LABELS)


def test_docs_enum_covers_every_facility_document():
    import seed
    assert {d for f in seed.FACILITIES for d in f["required_docs"]} == set(classify.DOCS)


REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
FAILURES = [
    ("timeout", FakeClient(exc=anthropic.APITimeoutError(request=REQ))),
    ("connection", FakeClient(exc=anthropic.APIConnectionError(request=REQ))),
    ("refusal after fallbacks", FakeClient(reply(GOOD, stop_reason="refusal"))),
    ("truncated", FakeClient(reply('{"barrier": "tra', stop_reason="max_tokens"))),
    ("not json", FakeClient(reply("transport"))),
    ("no credentials", FakeClient(exc=TypeError('"Could not resolve authentication method. Expected one of api_key"'))),
]


@pytest.mark.parametrize("cid,client", FAILURES, ids=[f[0] for f in FAILURES])
def test_failures_raise_unavailable(cid, client):
    with pytest.raises(classify.ClassifierUnavailable):
        classify.classify("sina doh ya mat", TODAY, client=client)


def test_bug_typeerror_is_not_swallowed():
    with pytest.raises(TypeError):
        classify.classify("x", TODAY, client=FakeClient(exc=TypeError("unexpected keyword argument")))


@pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="set RUN_LIVE=1 with API credentials to call Claude")
def test_live_smoke():
    out = classify.classify("ok i can go monday", TODAY)
    assert out["barrier"] in classify.BARRIERS


# ================================================================ scenario report
# Each scenario is one patient replying to the first SMS ("Is anything stopping you
# from going?"). It runs through eval.simulate() - keyword filter, exact-token rules,
# Claude, guards, rules table - exactly like a live SMS, but with real hospitals and
# a public place as the patient's location. Nothing is saved and no SMS is sent.

REPORT_DIR = ROOT / "eval" / "reports"
ALLOWED_PLACE_KINDS = {"park", "museum", "landmark", "restaurant", "station", "stadium",
                       "amusement park", "airport", "island landmark", "market", "mall"}


def load_scenarios():
    """tuning.jsonl rows with their report context filled in (DEFAULT_CONTEXT when absent)."""
    out = []
    for i, row in enumerate(ev.load("tuning")):
        hosp, place, trip = DEFAULT_CONTEXT[i % len(DEFAULT_CONTEXT)]
        if "hospital" in row:
            hosp, place, trip = row["hospital"], row.get("place"), row.get("trip", "-")
        out.append({"expect": row["label"], "text": row["text"], "fields": row.get("fields", {}),
                    "hospital": hosp, "place": place, "trip": trip, "note": row.get("note", ""),
                    "also_ok": row.get("also_ok", []), "lang": row.get("lang", "-"),
                    "edge": row.get("edge", False)})
    return out


SCENARIOS = load_scenarios()


def scenario_ids():
    counts, ids = {}, []
    for sc in SCENARIOS:
        prefix = "EDGE" if sc["edge"] else sc["expect"][:4].upper()
        counts[prefix] = counts.get(prefix, 0) + 1
        ids.append(f"{prefix}-{counts[prefix]:02d}")
    return ids


def build(i, sc):
    """Patient record, case, facility and the facility list for one scenario."""
    hosp = HOSPITALS[sc["hospital"]]
    city = [h for h in HOSPITALS.values() if h["city"] == hosp["city"]]
    place = PLACES.get(sc["place"]) if sc["place"] else None
    phone = (f"+1212555{100 + i % 100:04d}" if hosp["city"] == "New York"
             else f"+2540000{i:05d}")  # fictional / invalid ranges on purpose
    patient = {
        "patient_id": f"P-{i + 1:03d}", "name": NAMES[i % len(NAMES)], "phone": phone,
        "language": sc["lang"], "service": hosp["services"][0],
        "location": ({"name": place[0], "kind": place[1], "address": place[2]} if place else None),
        "trip": sc["trip"],
    }
    case = {"id": f"R-{9000 + i}", "service": patient["service"], "facility_id": hosp["id"],
            "area": hosp["area"], "patient_area": hosp["area"]}
    return patient, case, hosp, city, {"name": EMERGENCY[hosp["city"]]}


def run_scenario(i, sc, today, classify_fn, use_directions):
    import directions

    patient, case, hosp, city, emergency = build(i, sc)

    def directions_sms():
        if not (use_directions and patient["location"]):
            return None
        return directions.transport_sms(
            patient["location"]["address"], hosp["name"], hosp["address"], log=lambda *_: None)

    s = ev.simulate(sc["text"], case, hosp, today, classify_fn, facilities=city, emergency=emergency,
                    open_prompt="barrier_q", always_call_model=True, directions_fn=directions_sms)
    got = f"rule:{s.route}" if s.source == "rule" else s.label
    verdict = ("rule" if s.source == "rule" else "match" if got == sc["expect"]
               else "acceptable" if got in sc["also_ok"] else "different")
    return {"scenario": sc, "patient": patient, "hospital": hosp, "sim": s, "got": got, "verdict": verdict}


# "rule": a bare reply (e.g. "ok") answered by code before Claude is asked.
VERDICT = {"match": "OK", "acceptable": "ALSO OK", "different": "MISMATCH", "rule": "FIXED RULE"}
HANDLED = {"model": "Claude, then the safety checks",
           "model_error": "Claude failed, so a health worker takes over",
           "keyword": "clinical keyword (the live app skips Claude here)",
           "rule": "fixed rule, no Claude"}
RULE = "=" * 88


def _said(who, text):
    """'US       > first line' with following lines indented under the text."""
    lines = (text or "(nothing sent)").split("\n")
    pad = " " * 11
    return [f"{who:<8} > {lines[0]}"] + [f"{pad}{line}" for line in lines[1:]]


def _claude_line(s):
    if s.error:
        return f"Claude failed: {s.error}"
    if s.raw is None:
        return "Claude not asked"
    extra = []
    if s.raw.get("clinical_flag"):
        extra.append("clinical_flag")
    if s.raw.get("also_mentions"):
        extra.append("also mentions " + ", ".join(s.raw["also_mentions"]))
    if s.raw.get("fields"):
        extra.append("fields " + json.dumps(s.raw["fields"], ensure_ascii=False))
    return (f"Claude said {s.raw.get('barrier')}, confidence {s.raw.get('confidence')}"
            + (f" ({'; '.join(extra)})" if extra else "") + (f", {s.latency_s:.1f}s" if s.latency_s else ""))


def write_report(rows, ids, today, model_name, use_directions, out_dir=REPORT_DIR):
    """Plain-text report: one back-and-forth per scenario, summary first."""
    from collections import Counter
    from datetime import datetime

    import templates

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"classify-{datetime.now():%Y%m%d-%H%M%S}.txt"
    verdicts = Counter(r["verdict"] for r in rows)
    sources = Counter(r["sim"].source for r in rows)
    lat = sorted(r["sim"].latency_s for r in rows if r["sim"].latency_s is not None)

    L = [f"CLASSIFIER SCENARIO REPORT - {today:%a %Y-%m-%d}",
         f"Model {model_name} | {len(rows)} scenarios | directions "
         f"{'on (Google Maps)' if use_directions else 'off'} | generated {datetime.now():%H:%M}",
         "Hospital names and addresses are real; their hours, fees, documents and phone numbers are",
         "test values. Patient locations are public places, never homes. Names are fictional.", "",
         "RESULT     " + " | ".join(f"{VERDICT[v]} {verdicts.get(v, 0)}" for v in VERDICT),
         f"HANDLED BY Claude {sources.get('model', 0)} | clinical keyword {sources.get('keyword', 0)} | "
         f"fixed rule {sources.get('rule', 0)} | Claude failed {sources.get('model_error', 0)}"]
    if lat:
        L.append(f"LATENCY    median {lat[len(lat) // 2]:.1f}s, max {lat[-1]:.1f}s")
    L += ["", "BY EXPECTED LABEL"]
    for exp in sorted({r["scenario"]["expect"] for r in rows}, key=lambda e: (e.startswith("rule"), e)):
        c = Counter(r["verdict"] for r in rows if r["scenario"]["expect"] == exp)
        L.append(f"  {exp:<20} {sum(c.values()):>3} scenarios   OK {c['match']:>2}   "
                 f"ALSO OK {c['acceptable']:>2}   MISMATCH {c['different']:>2}")
    misses = [(sid, r) for sid, r in zip(ids, rows) if r["verdict"] == "different"]
    if misses:
        L += ["", "MISMATCHES"] + [f"  {sid:<9} should be {r['scenario']['expect']}, got {r['got']}: "
                                   f"\"{r['scenario']['text']}\"" for sid, r in misses]

    for sid, r in zip(ids, rows):
        sc, p, h, s = r["scenario"], r["patient"], r["hospital"], r["sim"]
        loc = p["location"]
        intro = templates.render(templates.PATIENT, "intro", name=h["name"], service=p["service"])
        L += ["", RULE, f"{sid:<12}{'[' + VERDICT[r['verdict']] + ']':>76}", RULE,
              f"PATIENT     {p['name']} ({p['patient_id']}) | {p['phone']} | language: {p['language']}",
              f"REFERRED    {p['service']} at {h['name']}, {h['address']}",
              "LOCATION    " + (f"{loc['name']} ({loc['kind']}), {loc['address']} | trip: {p['trip']}"
                                if loc else "not on file"),
              f"WHY         {sc['note'] or '-'}", ""]
        L += _said("US", intro) + _said("PATIENT", sc["text"]) + [""]
        L += [f"SHOULD BE       {sc['expect']}"
              + (f"   (also acceptable: {', '.join(sc['also_ok'])})" if sc["also_ok"] else ""),
              f"CLASSIFIED AS   {r['got']}   via {HANDLED[s.source]} | reason: {s.reason}",
              f"                {_claude_line(s)}", ""]
        L += _said("US", s.patient_sms)
        if s.help_sms:
            L += _said("US", s.help_sms)
        if s.clinic_sms:
            L += _said("CLINIC", s.clinic_sms)
        if s.escalate:
            L.append(f"HEALTH WORKER  case handed over ({s.escalate.replace('_', '-')})")

    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# ---- offline checks on the scenario data and the report (run by pytest)


def test_scenarios_cover_every_label_with_several_cases():
    for label in core.LABELS:
        assert sum(sc["expect"] == label for sc in SCENARIOS) >= 10, label


def test_scenarios_are_well_formed():
    for sc in SCENARIOS:  # labels themselves are checked by eval.load()
        assert set(sc["also_ok"]) <= set(core.LABELS), sc
        assert sc["hospital"] in HOSPITALS and (sc["place"] is None or sc["place"] in PLACES), sc
    for h in HOSPITALS.values():  # Claude may only name documents in classify.DOCS
        assert set(h["required_docs"]) <= set(classify.DOCS), h["name"]


def test_locations_are_public_places_not_homes():
    assert {kind for _, kind, _ in PLACES.values()} <= ALLOWED_PLACE_KINDS


def test_report_builds_offline(tmp_path):
    """Every scenario through the real pipeline, with Claude replaced by the gold label and fields."""
    by_text = {sc["text"]: sc for sc in SCENARIOS}

    def fake(text, today, meta):
        sc = by_text[text]
        return {"barrier": sc["expect"], "confidence": 0.9, "clinical_flag": sc["expect"] == "clinical_symptom",
                "also_mentions": [], "fields": sc["fields"]}

    rows = [run_scenario(i, sc, TODAY, fake, use_directions=False) for i, sc in enumerate(SCENARIOS)]
    assert all(r["sim"].patient_sms for r in rows)
    assert all(r["verdict"] in ("match", "rule") for r in rows)  # gold in -> gold out
    path = write_report(rows, scenario_ids(), TODAY, "fake", False, out_dir=tmp_path)
    assert path.suffix == ".txt"
    text = path.read_text(encoding="utf-8")
    for sid, sc in zip(scenario_ids(), SCENARIOS):  # each case: header, what they said, what should be
        assert f"\n{sid} " in text and sc["text"].split("\n")[0][:40] in text
    assert text.count("SHOULD BE ") == text.count("CLASSIFIED AS ") == len(SCENARIOS)


# ---- python tests/test_classify.py


def main(argv=None):
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    import envfile
    import eval as ev

    ap = argparse.ArgumentParser(description="Run the classifier scenarios through Claude and write a report.")
    ap.add_argument("--label", help="only scenarios expecting these labels, comma-separated "
                                    "(e.g. scheduling,missing_documents or rule:opt_out)")
    ap.add_argument("--limit", type=int, help="run at most N scenarios")
    ap.add_argument("--workers", type=int, default=6, help="parallel Claude calls (default 6)")
    ap.add_argument("--no-directions", action="store_true", help="skip Google Maps for transport replies")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    envfile.load()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env or your shell.")
    use_directions = not args.no_directions and bool(os.environ.get("GOOGLE_MAPS_API_KEY"))

    labels = {x.strip() for x in args.label.split(",")} if args.label else None
    picked = [(i, sid, sc) for i, (sid, sc) in enumerate(zip(scenario_ids(), SCENARIOS))
              if not labels or sc["expect"] in labels][: args.limit]
    if not picked:
        sys.exit(f"no scenarios for --label {args.label}")
    today, fn = date.today(), ev.classifier_fn()
    print(f"{len(picked)} scenarios · model {classify.model()} · directions {'on' if use_directions else 'off'}")

    def one(item):
        i, sid, sc = item
        r = run_scenario(i, sc, today, fn, use_directions)
        print(f"  {VERDICT[r['verdict']]:<8} {sid:9} {sc['expect']:18} -> {r['got']}")
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, picked))
    path = write_report(rows, [sid for _, sid, _ in picked], today, classify.model(), use_directions)
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
