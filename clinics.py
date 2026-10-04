"""The one list of clinics, loaded from clinics.json.

Both programs read it: the referral agent uses the clinics whose `programs`
include "referral" (via seed.FACILITIES), server.py looks a patient's clinic up
by id. Edit clinics.json, not this file.
"""

import json
from pathlib import Path

PATH = Path(__file__).parent / "clinics.json"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # weekday() order
REQUIRED = {"id", "programs", "name", "address", "phone"}
DEFAULTS = {"area": "", "services": [], "hours": {}, "hours_text": "", "required_docs": [],
            "cost_note": "", "transport_note": ""}


def load(path=PATH):
    clinics = []
    for raw in json.loads(Path(path).read_text(encoding="utf-8"))["clinics"]:
        missing = REQUIRED - set(raw)
        if missing:
            raise ValueError(f"{path}: clinic {raw.get('id', '?')} is missing {sorted(missing)}")
        bad_days = set(raw.get("hours", {})) - set(DAYS)
        if bad_days:
            raise ValueError(f"{path}: clinic {raw['id']} has unknown days {sorted(bad_days)}")
        c = {**DEFAULTS, **raw}
        # core.is_open_on() looks hours up by date.weekday(): Monday = 0.
        c["hours"] = {DAYS.index(day): span for day, span in c["hours"].items()}
        clinics.append(c)
    return clinics


ALL = load()
BY_ID = {c["id"]: c for c in ALL}


def for_program(name):
    return [c for c in ALL if name in c["programs"]]
