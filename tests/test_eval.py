"""The demo's headline numbers come from eval.py, so its scoring is tested."""

import json

import pytest

import classify
import core
import eval as ev


def fake(answers):
    """Classifier stand-in: text -> (barrier, confidence) or an exception."""
    def fn(text, today, meta):
        a = answers[text]
        if isinstance(a, Exception):
            raise a
        barrier, conf = a
        meta["usage"] = {"input_tokens": 100, "output_tokens": 20}
        return {"barrier": barrier, "confidence": conf, "clinical_flag": False,
                "also_mentions": [], "fields": {}}
    return fn


ROWS = [
    {"text": "my chest hurts", "label": "clinical_symptom"},             # keyword path
    {"text": "baby not feeding", "label": "clinical_symptom"},           # model misses it
    {"text": "no fare", "label": "transport"},                           # correct
    {"text": "how much?", "label": "cost"},                              # wrong workflow
    {"text": "eh", "label": "transport"},                                # low conf -> unknown (safe)
    {"text": "who?", "label": "unknown"},                                # API down -> CHW (safe)
]
ANSWERS = {
    "baby not feeding": ("fear_confusion", 0.8),
    "no fare": ("transport", 0.95),
    "how much?": ("transport", 0.9),
    "eh": ("cost", 0.4),
    "who?": classify.ClassifierUnavailable("APITimeoutError"),
}


@pytest.fixture
def results():
    return [ev.run_one(r, fake(ANSWERS)) for r in ROWS]


def test_sources(results):
    assert [r.source for r in results] == ["keyword", "model", "model", "model", "model", "model_error"]


def test_summary_counts(results):
    s = ev.summarize(results, budget_s=3.0)
    assert s["clinical_recall"] == "1/2"
    assert s["wrong_actions"] == "2/6"           # missed clinical + cost->transport
    assert s["label_accuracy"] == "2/6"
    assert s["safe_unknown"] == "1/6"            # "eh" (the timeout row's gold is unknown)
    assert s["model_errors"] == 1 and s["keyword_hits"] == 1
    assert (s["raw_errors_conf_ge_floor"], s["raw_errors_conf_lt_floor"]) == (2, 1)
    assert s["tokens_in"] == 400


def test_escalation_is_never_a_wrong_action(results):
    by_text = {r.text: r for r in results}
    assert not by_text["eh"].wrong_action        # unknown -> CHW
    assert not by_text["who?"].wrong_action      # outage -> urgent CHW (D6)
    assert by_text["who?"].pred_action == ("unknown", "clinical")


def test_heldout_needs_15_clinical(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path)
    rows = [{"text": f"m{i}", "label": "clinical_symptom" if i < 14 else "transport"} for i in range(50)]
    (tmp_path / "heldout.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(SystemExit, match="14 clinical"):
        ev.load("heldout")


def test_missing_heldout_explains_who_writes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path)
    with pytest.raises(SystemExit, match="teammate"):
        ev.load("heldout")


def test_tuning_set_is_well_formed():
    rows = ev.load("tuning")
    counts = {label: sum(r["label"] == label for r in rows) for label in core.LABELS}
    assert min(counts.values()) >= 10, counts
    assert counts["clinical_symptom"] >= 15 and counts["unknown"] >= 15
    assert len({r["text"].strip().lower() for r in rows}) == len(rows)  # no duplicate messages
    for row in rows:  # every gold row maps to a workflow, so gold actions are computable
        gold = core.Classification(row["label"], row.get("fields", {}))
        ev.action(gold)
    # Symptom messages the keyword list misses are what actually test the model.
    misses = [r for r in rows if r["label"] == "clinical_symptom" and not core.clinical_hit(r["text"])]
    assert len(misses) >= 5
