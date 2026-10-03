"""Demo data (fictional facilities, placeholder phone numbers).

Kept in code for v0; matches the judges' doc facility table.
"""

WEEKDAYS = {d: "08:00-17:00" for d in range(5)}

FACILITIES = [
    {"id": "FAC-A", "name": "Demo Mto Health Centre", "area": "Kayole",
     "services": ["antenatal", "hiv", "lab"], "hours": {**WEEKDAYS, 5: "08:00-12:00"},
     "hours_text": "Mon-Fri 8-5, Sat 8-12", "address": "Off Spine Rd, near Stage 2, Kayole",
     "phone": "+254700000101", "required_docs": ["referral letter", "national id", "clinic book"],
     "cost_note": "Consultation free; lab KES 200.", "transport_note": "Matatu 34 from CBD to Stage 2"},
    {"id": "FAC-B", "name": "Demo Baraka Clinic", "area": "Embakasi",
     "services": ["outpatient", "tb", "lab"], "hours": {d: "08:00-16:00" for d in range(5)},
     "hours_text": "Mon-Fri 8-4", "address": "Embakasi Rd, Embakasi", "phone": "+254700000102",
     "required_docs": ["referral letter", "national id"], "cost_note": "",
     "transport_note": "Matatu 33 from CBD"},
    {"id": "FAC-C", "name": "Demo Upendo Eye Unit", "area": "Westlands",
     "services": ["eye"], "hours": {1: "09:00-15:00", 3: "09:00-15:00"},
     "hours_text": "Tue & Thu 9-3", "address": "Westlands Rd, Westlands", "phone": "+254700000103",
     "required_docs": ["referral letter", "national id", "previous prescription"],
     "cost_note": "Screening free.", "transport_note": "Matatu 23 from CBD"},
    {"id": "FAC-D", "name": "Demo Faraja County Hospital", "area": "Kibera",
     "services": ["specialist", "maternity", "emergency", "lab"],
     "hours": {d: "00:00-24:00" for d in range(7)}, "hours_text": "Outpatient Mon-Sat 8-5; emergency 24 h",
     "address": "Kibera Dr, Kibera", "phone": "+254700000104",
     "required_docs": ["referral letter", "national id", "insurance card", "lab results"],
     "cost_note": "SHA accepted.", "transport_note": "Matatu 8 from CBD"},
]
FACILITY_BY_ID = {f["id"]: f for f in FACILITIES}
AREAS = sorted({f["area"] for f in FACILITIES})
EMERGENCY = {"name": "Demo Faraja County Hospital", "number": "+254700000104"}
CHWS = [{"id": "CHW-1", "name": "Demo CHW Amina", "phone": "+254700000201"}]

DEMO_PATIENT_PHONE = "+254700000301"
DEMO_CASE = {"id": "R-0142", "patient_phone": DEMO_PATIENT_PHONE, "patient_area": "Embakasi",
             "service": "lab", "facility_id": "FAC-B"}
