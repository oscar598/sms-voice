import json
import os
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import classify
import core

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
    assert req["model"] == classify.MODEL
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["schema"]["properties"]["barrier"]["enum"] == classify.BARRIERS
    assert "2026-10-05 (Monday)" in req["messages"][0]["content"]  # relative dates resolve


def test_schema_enum_matches_core_labels():
    assert set(classify.BARRIERS) == core.LABELS


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
