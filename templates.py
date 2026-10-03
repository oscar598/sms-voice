"""Approved outbound SMS templates. The model never writes patient-facing text.

Slots are filled only from the facility table or validated fields (core.py).
Patient templates must stay within one GSM-7 segment under the trial prefix
(<=120 chars); the length test is T5.
"""

PATIENT = {
    "transport": "To reach {name}: {transport_note}. Address: {address}.",
    "clinic_hours": "{name} is open {hours}. Reply here if you still can't get seen.",
    "clinic_alternative": "{name} is closed today. {alt_name} offers this, open {alt_hours}. {alt_address}.",
    "turned_away": "We've told {name} you're coming on {date}.",
    "missing_documents": "For {name} bring: {required_docs}.",
    "scheduling": "Book at {name}: call {phone}, open {hours}.",
    "scheduling_set": "Thanks! We've told {name} you're coming on {date}.",
    "wrong_facility": "{alt_name} offers this, open {alt_hours}. {alt_address}.",
    "fear_confusion": "You were sent to {name} for {service}. It is a check-up, not an emergency.",
    "language_sw": "Umetumwa {name} kwa {service}. Saa za kazi: {hours}.",
    "cost": "{cost_note}",
    "clinical": "A health worker will contact you now. If severe, go to {emergency_name} or call {emergency_number}.",
    "plan_ack": "Thanks! See you at {name} on {date}.",
    "plan_ack_nodate": "Thanks!",
    "unknown": "Thanks, a health worker will follow up.",
    "holding": "Sorry, replies are slow right now. A health worker will get back to you.",
}

CLINIC = {
    "arriving": "{case_id} arriving {date}. Reply Y {case_id} when seen.",
}


def render(table, key, **slots):
    return table[key].format(**slots)
