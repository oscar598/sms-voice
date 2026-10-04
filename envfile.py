"""Load KEY=VALUE lines from .env into the environment (entry points only).

Called by `python app.py` and `python eval.py`, never by tests, so a real API
key in .env cannot leak into the test run. Variables already set in the
environment win over the file.
"""

import os
from pathlib import Path

DEFAULT = Path(__file__).parent / ".env"


def parse(text):
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load(path=DEFAULT):
    path = Path(path)
    if not path.exists():
        return {}
    loaded = {k: v for k, v in parse(path.read_text(encoding="utf-8")).items() if v and k not in os.environ}
    os.environ.update(loaded)
    return loaded
