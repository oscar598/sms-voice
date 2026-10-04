import json

import pytest

import clinics
import seed


def test_referral_clinics_come_from_the_shared_list():
    assert [f["id"] for f in seed.FACILITIES] == ["FAC-A", "FAC-B", "FAC-C", "FAC-D"]
    assert all("referral" in f["programs"] for f in seed.FACILITIES)
    assert seed.EMERGENCY == {"name": "Demo Faraja County Hospital"}  # the clinic offering emergency


def test_hours_use_weekday_numbers():
    fac_c = clinics.BY_ID["FAC-C"]  # Tue & Thu only
    assert fac_c["hours"] == {1: "09:00-15:00", 3: "09:00-15:00"}


def test_ids_are_unique():
    assert len(clinics.BY_ID) == len(clinics.ALL)


@pytest.mark.parametrize("clinic,error", [
    ({"id": "X", "programs": [], "name": "X", "address": "a"}, "missing"),          # no phone
    ({"id": "X", "programs": [], "name": "X", "address": "a", "phone": "1",
      "hours": {"monday": "08:00-17:00"}}, "unknown days"),
])
def test_bad_entries_are_rejected(tmp_path, clinic, error):
    path = tmp_path / "clinics.json"
    path.write_text(json.dumps({"clinics": [clinic]}))
    with pytest.raises(ValueError, match=error):
        clinics.load(path)


def test_server_finds_clinic_by_column_then_default(monkeypatch):
    import server
    monkeypatch.setattr(server, "DEFAULT_CLINIC_ID", "NY-SINAI")
    assert server.clinic_for({"clinic_id": "FAC-A"})["name"] == "Demo Mto Health Centre"
    assert server.clinic_for({})["id"] == "NY-SINAI"
    assert server.clinic_for({"clinic_id": "NOPE"}) is None
