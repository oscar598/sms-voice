"""Approved outbound SMS templates. The model never writes patient-facing text.

Slots are filled only from the facility table or validated fields (core.py).
Patient templates must stay within one GSM-7 segment under the trial prefix
(<=120 chars); the length test is T5.
"""

PATIENT = {
    "transport": "To reach {name}: {transport_note}. Address: {address}.",
    "clinic_hours": "{name} is open {hours}. Reply here if you still can't get seen.",
    "clinic_alternative": "Closed today. Try {alt_name}, {alt_address}. Open {alt_hours}.",
    "missing_documents": "For {name} bring: {required_docs}.",
    "scheduling": "Book at {name}: call {phone}, open {hours}.",
    "scheduling_set": "Thanks! We've told {name} you're coming on {date}.",
    "wrong_facility": "{alt_name} offers this, open {alt_hours}. {alt_address}.",
    "fear_confusion": "You were sent to {name} for {service}. It is a check-up, not an emergency.",
    "language_sw": "Umetumwa {name} kwa {service}. Saa za kazi: {hours}.",
    "cost": "{cost_note}",
    "clinical": "A health worker will contact you now. In an urgent emergency, call 911 or go to {emergency_name}.",
    "plan_ack": "Thanks! See you at {name} on {date}.",
    "plan_ack_nodate": "Thanks!",
    "unknown": "Thanks, a health worker will follow up.",
    "holding": "Sorry, replies are slow right now. A health worker will get back to you.",
}

PATIENT["followup"] = "Did you get seen at {name}? Reply YES or NO."
PATIENT["reminder"] = "Is anything stopping you from going to {name}? Reply here."
PATIENT["what_happened"] = "Sorry to hear that. What got in the way? Reply here."
PATIENT["ask_barrier"] = "What is stopping you from going to {name}? E.g. transport, cost, hours, documents. Reply here."
PATIENT["ask_date"] = "Great! What day will you go to {name}? Reply with the day."
PATIENT["ask_again"] = "Is anything stopping you from going to {name}? Reply YES or NO."
PATIENT["clarify"] = "Sorry, I didn't get that. What is stopping you from going to {name}? Reply here."
PATIENT["which_barrier"] = "Which is the biggest problem right now? Reply with one, e.g. transport or cost."
PATIENT["chw_joined"] = "A health worker is now on this chat. Reply here to talk to them."
PATIENT["intro"] = "Hi! You were referred to {name} for {service}. Is anything stopping you from going? Reply here."

CLINIC = {
    "arriving": "{case_id} arriving {date}. Reply Y {case_id} when seen.",
    "followup": "Was {case_id} seen? Reply Y {case_id} or N {case_id}.",
}

CHW = {
    # Every SMS fits SMS_MAX_CHARS (sms.py), CHW ones included; the baton quote is cut to 60.
    "baton": "{kind} {case_id} ({phone}): \"{quote}\". Reply 1 {case_id} to take it.",
    "claimed": "{case_id} is yours. Texts you send here now go to the patient. Reply DONE {case_id} or LOST {case_id} when finished.",
    "from_patient": "{case_id} patient: \"{quote}\"",
    "which": "Which case? Reply with the case id, e.g. 1 R-0142.",
    "not_open": "{case_id} has nothing waiting for a health worker.",
    "done": "{case_id} closed. Automated follow-up for the patient resumes.",
    "lost": "{case_id} marked lost. No more messages will go to the patient.",
    "help": "Commands: 1 R-0142 to take a case, DONE R-0142 when finished, LOST R-0142 if unreachable. Once a case is yours, other texts go to the patient.",
    "many": "You hold more than one case. Start with the case id: R-0142 your message.",
}

# Prefix on a CHW's own words relayed to the patient (human-written, not a template).
# Relayed words get whatever room SMS_MAX_CHARS leaves (app._with_quote).
CHW_RELAY = "Health worker: "

STAFF_WHICH = "Which case? Reply with the case id, e.g. Y R-0142."

# Second SMS after every barrier reply, the same for all barriers (core.help_sms),
# so the first SMS keeps the whole SMS_MAX_CHARS for the personalized answer.
HELP_SMS = ("If you need more help, please call us at {phone}. Someone here will be happy to help. "
            "Is there another question we can answer for you?")
CLINIC_HELP = "Reply Y R-0142 if the patient was seen, N R-0142 if not."


def render(table, key, **slots):
    return table[key].format(**slots)
