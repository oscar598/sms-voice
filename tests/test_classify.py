"""classify.py unit tests, plus a scenario report generator.

    pytest                                  # unit tests only (no API calls)
    python tests/test_classify.py           # run every scenario through Claude -> eval/reports/
    python tests/test_classify.py --label transport --limit 5 --workers 4 --no-directions
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
from classify_scenarios import EMERGENCY, HOSPITALS, NAMES, PLACES, SCENARIOS  # noqa: E402

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
    import eval as ev
    import templates

    patient, case, hosp, city, emergency = build(i, sc)

    def directions_sms():
        if not (use_directions and patient["location"]):
            return None
        return directions.transport_sms(
            patient["location"]["address"], hosp["name"], hosp["address"], log=lambda *_: None,
            footer=templates.TRANSPORT_HELP.format(phone=hosp["phone"]))

    s = ev.simulate(sc["text"], case, hosp, today, classify_fn, facilities=city, emergency=emergency,
                    open_prompt="barrier_q", always_call_model=True, directions_fn=directions_sms)
    got = f"rule:{s.route}" if s.source == "rule" else s.label
    verdict = "match" if got == sc["expect"] else "acceptable" if got in sc["also_ok"] else "different"
    return {"scenario": sc, "patient": patient, "hospital": hosp, "sim": s, "got": got, "verdict": verdict}


MARK = {"match": "✅", "acceptable": "🟡", "different": "❌"}


def _md(text):
    return (text or "").replace("|", "\\|").replace("\n", "<br>")


def write_report(rows, ids, today, model_name, use_directions, out_dir=REPORT_DIR):
    """Markdown report (read it in VS Code's preview) + one JSON line per scenario."""
    from collections import Counter
    from dataclasses import asdict
    from datetime import datetime

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path, jsonl_path = out_dir / f"classify-{stamp}.md", out_dir / f"classify-{stamp}.jsonl"
    verdicts = Counter(r["verdict"] for r in rows)
    sources = Counter(r["sim"].source for r in rows)
    lat = sorted(r["sim"].latency_s for r in rows if r["sim"].latency_s is not None)
    tokens = [sum(r["sim"].usage.get(k, 0) for r in rows) for k in ("input_tokens", "output_tokens")]

    L = [f"# Classifier scenario report ({today:%a %Y-%m-%d})", "",
         f"Model `{model_name}` · {len(rows)} scenarios · directions "
         f"{'on (Google Maps)' if use_directions else 'off'} · generated {datetime.now():%H:%M}", "",
         "Hospital names and addresses are real; their hours, fees, documents and phone numbers are "
         "test values. Patient locations are public places, never homes. Names are fictional.", "",
         "**Verdicts:** " + " · ".join(f"{MARK[v]} {v} {verdicts.get(v, 0)}" for v in MARK),
         f"**How each message was handled:** sent to Claude {sources.get('model', 0)} · clinical keyword "
         f"{sources.get('keyword', 0)} · fixed rule {sources.get('rule', 0)} · Claude failed "
         f"{sources.get('model_error', 0)}",
         f"**Claude latency:** median {lat[len(lat) // 2]:.1f}s, max {lat[-1]:.1f}s · tokens in {tokens[0]}, "
         f"out {tokens[1]}" if lat else "**Claude latency:** no calls", ""]

    L += ["## By expected label", "", "| Expected | Scenarios | ✅ | 🟡 | ❌ |", "|---|---|---|---|---|"]
    for exp in sorted({r["scenario"]["expect"] for r in rows}, key=lambda e: (e.startswith("rule"), e)):
        group = [r for r in rows if r["scenario"]["expect"] == exp]
        c = Counter(r["verdict"] for r in group)
        L.append(f"| `{exp}` | {len(group)} | {c['match']} | {c['acceptable']} | {c['different']} |")

    L += ["", "## All scenarios", "", "| ID | Patient | Location | Message | Expected | Got | |",
          "|---|---|---|---|---|---|---|"]
    for sid, r in zip(ids, rows):
        loc = r["patient"]["location"]
        L.append(f"| [{sid}](#{sid.lower()}) | {_md(r['patient']['name'])} | "
                 f"{_md(loc['name']) if loc else '(none)'} ({r['patient']['trip']}) | {_md(r['scenario']['text'])} | "
                 f"`{r['scenario']['expect']}` | `{r['got']}` | {MARK[r['verdict']]} |")

    L += ["", "## Details", ""]
    for sid, r in zip(ids, rows):
        sc, p, h, s = r["scenario"], r["patient"], r["hospital"], r["sim"]
        loc = p["location"]
        L += [f"### {sid}", "",
              f"{MARK[r['verdict']]} expected `{sc['expect']}`, got `{r['got']}`"
              + (f" (also acceptable: {', '.join(sc['also_ok'])})" if sc["also_ok"] else ""), "",
              f"- **Client:** {p['name']} ({p['patient_id']}) · {p['phone']} · language `{p['language']}`",
              f"- **Referral:** {p['service']} at {h['name']}, {h['address']}",
              f"- **Location:** " + (f"{loc['name']} ({loc['kind']}), {loc['address']} · trip: {p['trip']}"
                                     if loc else "not on file"),
              f"- **Why this case:** {sc['note'] or '-'}", "",
              f"**Patient wrote:** {sc['text']}", ""]
        if s.raw is not None:
            L.append(f"**Claude's answer** ({s.latency_s:.1f}s): `{json.dumps(s.raw, ensure_ascii=False)}`")
        elif s.error:
            L.append(f"**Claude failed:** {s.error}")
        handled = {"model": "Claude's label, after the safety checks",
                   "model_error": "Claude failed, so it goes to a health worker",
                   "keyword": "clinical keyword - the live app skips Claude here",
                   "rule": "fixed rule, no Claude"}[s.source]
        L += ["", f"**Decision:** `{r['got']}` - {handled} (reason: {s.reason})", "",
              "**Patient SMS:**", "", "> " + (s.patient_sms or "(nothing sent)").replace("\n", "  \n> "), ""]
        if s.clinic_sms:
            L += [f"**Clinic SMS:** {s.clinic_sms}", ""]
        if s.escalate:
            L += [f"**Health worker:** case handed over ({s.escalate.replace('_', '-')})", ""]

    md_path.write_text("\n".join(L), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for sid, r in zip(ids, rows):
            f.write(json.dumps({"id": sid, "verdict": r["verdict"], "got": r["got"], **r["scenario"],
                                "patient": r["patient"], "hospital": r["hospital"]["name"],
                                "result": asdict(r["sim"])}, ensure_ascii=False) + "\n")
    return md_path, jsonl_path


# ---- offline checks on the scenario data and the report (run by pytest)


def test_scenarios_cover_every_label_with_several_cases():
    for label in core.LABELS:
        assert sum(sc["expect"] == label for sc in SCENARIOS) >= 5, label


def test_scenarios_are_well_formed():
    for sc in SCENARIOS:
        assert sc["expect"] in core.LABELS or sc["expect"].startswith("rule:"), sc
        assert set(sc["also_ok"]) <= set(core.LABELS), sc
        assert sc["hospital"] in HOSPITALS and (sc["place"] is None or sc["place"] in PLACES), sc
    for h in HOSPITALS.values():  # Claude may only name documents in classify.DOCS
        assert set(h["required_docs"]) <= set(classify.DOCS), h["name"]


def test_locations_are_public_places_not_homes():
    assert {kind for _, kind, _ in PLACES.values()} <= ALLOWED_PLACE_KINDS


def test_report_builds_offline(tmp_path):
    """Every scenario through the real pipeline with Claude replaced by its expected label."""
    def fake(text, today, meta):
        sc = next(s for s in SCENARIOS if s["text"] == text)
        label = sc["expect"] if sc["expect"] in core.LABELS else "unknown"
        return {"barrier": label, "confidence": 0.9, "clinical_flag": label == "clinical_symptom",
                "also_mentions": [], "fields": {}}

    rows = [run_scenario(i, sc, TODAY, fake, use_directions=False) for i, sc in enumerate(SCENARIOS)]
    assert all(r["sim"].patient_sms or r["got"] == "rule:opt_out" for r in rows)
    assert {r["got"] for r in rows if r["scenario"]["expect"].startswith("rule:")} == {
        sc["expect"] for sc in SCENARIOS if sc["expect"].startswith("rule:")}
    md, jsonl = write_report(rows, scenario_ids(), TODAY, "fake", False, out_dir=tmp_path)
    text = md.read_text(encoding="utf-8")
    assert all(f"### {sid}" in text for sid in scenario_ids())
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == len(SCENARIOS)


# ---- python tests/test_classify.py


def main(argv=None):
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    import envfile
    import eval as ev

    ap = argparse.ArgumentParser(description="Run the classifier scenarios through Claude and write a report.")
    ap.add_argument("--label", help="only scenarios expecting this label (e.g. transport, rule:opt_out)")
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

    picked = [(i, sid, sc) for i, (sid, sc) in enumerate(zip(scenario_ids(), SCENARIOS))
              if not args.label or sc["expect"] == args.label][: args.limit]
    if not picked:
        sys.exit(f"no scenarios for --label {args.label}")
    today, fn = date.today(), ev.classifier_fn()
    print(f"{len(picked)} scenarios · model {classify.model()} · directions {'on' if use_directions else 'off'}")

    def one(item):
        i, sid, sc = item
        r = run_scenario(i, sc, today, fn, use_directions)
        print(f"  {MARK[r['verdict']]} {sid:8} {sc['expect']:18} -> {r['got']}")
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, picked))
    md, jsonl = write_report(rows, [sid for _, sid, _ in picked], today, classify.model(), use_directions)
    print(f"\nReport: {md}\nData:   {jsonl}")


if __name__ == "__main__":
    main()
