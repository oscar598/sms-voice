"""Context for the scenario report (python tests/test_classify.py).

The messages and their labels are eval/tuning.jsonl - the same set eval.py
scores. A row may name a hospital, place and trip below; rows that don't (the
original Nairobi tuning messages) get DEFAULT_CONTEXT in turn.

Hospital names and addresses are real public addresses. Their hours, fees,
documents and phone numbers are TEST VALUES, not the hospitals' real details.
Patient locations are public places (parks, landmarks, restaurants, stations),
never homes. Names are fictional; phone numbers use reserved fictional ranges.
"""

WEEKDAYS = {d: "08:00-17:00" for d in range(5)}

HOSPITALS = {
    # ---- New York
    "sinai": {"id": "NY-SINAI", "city": "New York", "area": "East Harlem",
              "name": "Mount Sinai Hospital", "address": "1 Gustave L. Levy Place, New York, NY 10029",
              "services": ["lab", "cardiology", "maternity", "eye"], "hours": {**WEEKDAYS, 5: "09:00-13:00"},
              "hours_text": "Mon-Fri 8-5, Sat 9-1", "phone": "+12125550170",
              "required_docs": ["referral letter", "insurance card"],
              "cost_note": "Ask us about financial assistance before your visit.",
              "transport_note": "call us for directions"},
    "nyu": {"id": "NY-NYU", "city": "New York", "area": "Kips Bay",
            "name": "NYU Langone Tisch Hospital", "address": "550 First Avenue, New York, NY 10016",
            "services": ["lab", "cardiology", "orthopedics"], "hours": dict(WEEKDAYS),
            "hours_text": "Mon-Fri 8-5", "phone": "+12125550171",
            "required_docs": ["referral letter", "insurance card", "lab results"],
            "cost_note": "", "transport_note": "call us for directions"},
    "bellevue": {"id": "NY-BELLEVUE", "city": "New York", "area": "Kips Bay",
                 "name": "Bellevue Hospital", "address": "462 First Avenue, New York, NY 10016",
                 "services": ["lab", "outpatient", "tb", "emergency"],
                 "hours": {d: "00:00-24:00" for d in range(7)}, "hours_text": "Clinics Mon-Fri 8-5; ER 24 h",
                 "phone": "+12125550172", "required_docs": ["referral letter"],
                 "cost_note": "Care is provided regardless of ability to pay.",
                 "transport_note": "call us for directions"},
    "columbia": {"id": "NY-COLUMBIA", "city": "New York", "area": "Washington Heights",
                 "name": "NewYork-Presbyterian/Columbia University Irving Medical Center",
                 "address": "622 West 168th Street, New York, NY 10032",
                 "services": ["lab", "eye", "maternity", "cardiology"], "hours": {**WEEKDAYS, 5: "08:00-12:00"},
                 "hours_text": "Mon-Fri 8-5, Sat 8-12", "phone": "+12125550173",
                 "required_docs": ["referral letter", "insurance card", "previous prescription"],
                 "cost_note": "Ask us about financial assistance before your visit.",
                 "transport_note": "call us for directions"},
    # ---- Nairobi
    "knh": {"id": "KE-KNH", "city": "Nairobi", "area": "Upper Hill",
            "name": "Kenyatta National Hospital", "address": "Hospital Road, Upper Hill, Nairobi",
            "services": ["lab", "specialist", "maternity", "emergency"],
            "hours": {d: "00:00-24:00" for d in range(7)}, "hours_text": "Clinics Mon-Fri 8-5; casualty 24 h",
            "phone": "+254000000701", "required_docs": ["referral letter", "national id"],
            "cost_note": "SHA accepted.", "transport_note": "call us for directions"},
    "agakhan": {"id": "KE-AGAKHAN", "city": "Nairobi", "area": "Parklands",
                "name": "Aga Khan University Hospital", "address": "3rd Parklands Avenue, Nairobi",
                "services": ["lab", "cardiology", "eye"], "hours": {**WEEKDAYS, 5: "08:00-13:00"},
                "hours_text": "Mon-Fri 8-5, Sat 8-1", "phone": "+254000000702",
                "required_docs": ["referral letter", "national id", "insurance card"],
                "cost_note": "", "transport_note": "call us for directions"},
    "nbihosp": {"id": "KE-NBIHOSP", "city": "Nairobi", "area": "Hurlingham",
                "name": "The Nairobi Hospital", "address": "Argwings Kodhek Road, Nairobi",
                "services": ["lab", "outpatient", "maternity"], "hours": dict(WEEKDAYS),
                "hours_text": "Mon-Fri 8-5", "phone": "+254000000703",
                "required_docs": ["referral letter", "national id", "insurance card"],
                "cost_note": "Insurance and SHA accepted.", "transport_note": "call us for directions"},
    "mbagathi": {"id": "KE-MBAGATHI", "city": "Nairobi", "area": "Mbagathi",
                 "name": "Mbagathi County Hospital", "address": "Mbagathi Way, Nairobi",
                 "services": ["tb", "hiv", "outpatient", "lab"], "hours": dict(WEEKDAYS),
                 "hours_text": "Mon-Fri 8-5", "phone": "+254000000704",
                 "required_docs": ["referral letter", "national id", "clinic book"],
                 "cost_note": "Consultation free at county facilities.", "transport_note": "call us for directions"},
}

# Per city: the hospital the clinical SMS points to.
EMERGENCY = {"New York": "Bellevue Hospital", "Nairobi": "Kenyatta National Hospital"}

PLACES = {
    # ---- New York
    "conservatory": ("Central Park Conservatory Garden", "park", "Fifth Avenue & East 105th Street, New York, NY 10029"),
    "met": ("The Metropolitan Museum of Art", "museum", "1000 Fifth Avenue, New York, NY 10028"),
    "empire": ("Empire State Building", "landmark", "350 Fifth Avenue, New York, NY 10118"),
    "madison_sq": ("Madison Square Park", "park", "Madison Avenue & East 23rd Street, New York, NY 10010"),
    "katz": ("Katz's Delicatessen", "restaurant", "205 East Houston Street, New York, NY 10002"),
    "grand_central": ("Grand Central Terminal", "station", "89 East 42nd Street, New York, NY 10017"),
    "yankee": ("Yankee Stadium", "stadium", "1 East 161st Street, Bronx, NY 10451"),
    "cloisters": ("The Met Cloisters", "museum", "99 Margaret Corbin Drive, New York, NY 10040"),
    "coney": ("Luna Park at Coney Island", "amusement park", "1000 Surf Avenue, Brooklyn, NY 11224"),
    "jfk": ("JFK Airport Terminal 4", "airport", "Terminal 4, Jamaica, NY 11430"),
    "liberty": ("Statue of Liberty", "island landmark", "Liberty Island, New York, NY 10004"),
    "reading": ("Reading Terminal Market", "market", "1136 Arch Street, Philadelphia, PA 19107"),
    # ---- Nairobi
    "uhuru": ("Uhuru Park", "park", "Kenyatta Avenue, Nairobi"),
    "museum": ("Nairobi National Museum", "museum", "Museum Hill Road, Nairobi"),
    "yaya": ("Yaya Centre", "mall", "Argwings Kodhek Road, Nairobi"),
    "kicc": ("Kenyatta International Convention Centre", "landmark", "Harambee Avenue, Nairobi"),
    "carnivore": ("Carnivore Restaurant", "restaurant", "Langata Road, Nairobi"),
    "tworivers": ("Two Rivers Mall", "mall", "Limuru Road, Nairobi"),
    "karura": ("Karura Forest", "park", "Limuru Road, Nairobi"),
    "gikomba": ("Gikomba Market", "market", "Gikomba, Nairobi"),
    "jkia": ("Jomo Kenyatta International Airport", "airport", "Airport North Road, Nairobi"),
    "fortjesus": ("Fort Jesus", "landmark", "Nkrumah Road, Mombasa"),
}

NAMES = [
    "Maya Thompson", "Luis Ortega", "Wanjiru Kamau", "Otieno Odhiambo", "Chen Wei", "Fatou Diop",
    "Priya Raman", "Samuel Mensah", "Elena Petrova", "Grace Njeri", "Amina Yusuf", "Kevin Mwangi",
    "Rosa Delgado", "Hana Sato", "Ibrahim Hassan", "Zawadi Achieng", "Daniel Cohen", "Mei Lin",
    "Joseph Kiptoo", "Lucia Fernandes", "Omar Farouk", "Brigid O'Neill", "Kofi Boateng",
    "Nadia Rahman", "Peter Waweru", "Sofia Rossi", "Ahmed Ali", "Faith Wambui", "Tomasz Nowak",
    "Aisha Bello",
]

# (hospital, place, trip) for tuning rows without their own context: Nairobi, like the messages.
DEFAULT_CONTEXT = [
    ("knh", "uhuru", "near"), ("mbagathi", "gikomba", "across town"), ("nbihosp", "yaya", "near"),
    ("agakhan", "museum", "near"), ("knh", "carnivore", "across town"), ("agakhan", "tworivers", "across town"),
    ("nbihosp", "karura", "across town"), ("knh", "kicc", "near"), ("mbagathi", "jkia", "far"),
]
