"""Approved outbound SMS templates. The model never writes patient-facing text.

Slots are filled only from the facility table or validated fields (core.py).
Patient templates must stay within one GSM-7 segment under the trial prefix
(<=120 chars); the length test is T5.
"""

PATIENT = {
    "transport": "To reach {name}: {transport_note}. Address: {address}.",
    "clinic_hours": "{name} is open {hours}. Reply here if you still can't get seen.",
    "clinic_alternative": "Closed today. Try {alt_name}, {alt_address}. Open {alt_hours}.",
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

PATIENT["followup"] = "Did you get seen at {name}? Reply YES or NO."
PATIENT["reminder"] = "Is anything stopping you from going to {name}? Reply here."
PATIENT["what_happened"] = "Sorry to hear that. What got in the way? Reply here."
PATIENT["intro"] = "Hi! You were referred to {name} for {service}. Is anything stopping you from going? Reply here."

CLINIC = {
    "arriving": "{case_id} arriving {date}. Reply Y {case_id} when seen.",
    "followup": "Was {case_id} seen? Reply Y {case_id} or N {case_id}.",
}

CHW = {
    # CHW/supervisor SMS may run to 2 segments (D8); the patient quote is cut to 60.
    "baton": "{kind} {case_id} ({phone}): \"{quote}\". Reply 1 {case_id} to take it.",
    "claimed": "{case_id} is yours. Reply DONE {case_id} or LOST {case_id} when finished.",
    "which": "Which case? Reply with the case id, e.g. 1 R-0142.",
    "not_open": "{case_id} has nothing waiting for a health worker.",
    "done": "{case_id} closed. Automated follow-up for the patient resumes.",
    "lost": "{case_id} marked lost. No more messages will go to the patient.",
    "help": "Commands: 1 R-0142 to take a case, DONE R-0142 when finished, LOST R-0142 if unreachable. Call the patient to talk.",
}

STAFF_WHICH = "Which case? Reply with the case id, e.g. Y R-0142."
CLINIC_HELP = "Reply Y R-0142 if the patient was seen, N R-0142 if not."


def render(table, key, **slots):
    return table[key].format(**slots)
