# SMS Voice: referral closure agent

## The problem

A health worker refers a patient to a clinic, and then often nothing happens. The patient runs into a barrier: no fare for the matatu, a missing document, a locked gate, a front desk that sends them home. Nobody finds out which barrier it was. The community health worker (CHW) who made the referral usually never learns whether the patient arrived. Nobody owns the gap between "referred" and "seen".

## How this project solves it

This is an SMS agent that follows every referral until the visit happens. It runs over plain SMS, so it works on a basic feature phone with no app and no data plan. For each referred patient it:

1. **Asks.** It texts the patient and finds out *why* the referral is stuck.
2. **Acts.** It takes the smallest permitted administrative step: directions, a document checklist, the clinic's posted hours, another clinic that offers the service, or a text telling the clinic the patient is coming.
3. **Escalates.** If the patient mentions a symptom, or the agent is unsure, a CHW gets the case by SMS together with the patient's own words. The CHW can claim the case and chat with the patient directly.
4. **Confirms.** After the visit date it asks both the patient and the clinic whether the patient was seen. A "yes" from the clinic counts as the stronger confirmation.

**Clinic Reliability Radar.** Every barrier is logged against the clinic it happened at. The dashboard can then show, for example, that many patients referred to one clinic found it closed or were turned away. Because this rests on what patients report, it is shown as a signal that needs follow-up, not as a score.

**Safety model: Claude classifies, code decides.** Claude reads each free-text patient message and returns one label from a fixed list, such as `transport`, `cost`, `clinic_closed`, `turned_away`, `language`, `clinical_symptom`, `plan_ack` or `unknown`. Deterministic code then picks the action:

- Patients only receive pre-written, approved templates. They never receive text written by the model.
- Clinical keywords are checked before Claude is called. Any symptom sends the case to a human.
- A low-confidence answer, a message with several barriers, an invalid response or an API failure never produces a guessed action. The agent asks again or hands the case to a CHW.
- The agent never diagnoses and never gives medical advice.
- After `STOP` the patient receives nothing at all until they send `START`.

The full design and review decisions are in [docs/designs/referral-agent.md](docs/designs/referral-agent.md).

## Project structure

```
sms-voice/
├── app.py                  # Flask app: /sms webhook, /sim, /api/*, referral form, sending SMS
├── core.py                 # Router, clinical keyword filter, guards, rules table (no I/O)
├── classify.py             # The only Claude call: labels one patient SMS as JSON
├── scheduler.py            # Reminders, follow-ups, completion timers (runs on each request)
├── db.py                   # SQLite schema and queries
├── templates.py            # Every SMS the system can send
├── metrics.py              # Dashboard numbers and the clinic reliability radar
├── seed.py                 # Synthetic Nairobi clinics, CHW and 40 demo cases
├── eval.py                 # Classifier evaluation against labelled messages
├── try_claude.py           # Send one message to Claude from the command line, print the decision
├── envfile.py              # Loads .env for app.py, eval.py and try_claude.py
├── home.html               # Landing page (/)
├── referral.html           # New-referral form (/referrals/new)
├── sim.html                # Simulated patient, CHW and clinic phones (/sim)
├── eval/
│   ├── tuning.jsonl            # Labelled messages used to tune the prompt
│   └── heldout.TEMPLATE.jsonl  # Format for the held-out set a teammate writes
├── tests/                  # pytest unit tests (no API key or network needed)
│   ├── test_app.py
│   ├── test_classify.py
│   ├── test_core.py
│   ├── test_dashboard.py
│   ├── test_envfile.py
│   ├── test_eval.py
│   ├── test_inbound.py
│   ├── test_referrals.py
│   ├── test_scheduler.py
│   ├── test_send.py
│   └── test_templates.py
├── docs/
│   ├── designs/referral-agent.md   # Design doc and engineering review
│   └── lovable-dashboard-prompt.md # Prompt used to build the hosted dashboard
├── render.yaml             # Render deploy blueprint
├── tunnel.sh               # ngrok tunnel on a fixed domain, restarted automatically
├── requirements.txt
├── pytest.ini
└── CLAUDE.md
```

## Running it

### Option A: use the hosted demo

| Site | URL | What it is |
|---|---|---|
| Home | https://sms-voice-referral.onrender.com/ | Links to everything below |
| Simulator | https://sms-voice-referral.onrender.com/sim | Three fake phones (patient, CHW, clinic). Password protected |
| New referral | https://sms-voice-referral.onrender.com/referrals/new | Create a referral. Needs the referral token |
| Dashboard | https://pixel-perfect-showcase-4501.lovable.app/ | Completion rate, top barriers and the clinic reliability radar |

The hosted service runs in simulator mode, so **no real SMS is sent**. Every message appears on screen.

### Testing Claude in the simulator

1. Open https://sms-voice-referral.onrender.com/sim. The browser asks for a login. Any username works; the password is the `SIM_PASSWORD` set in Render (ask the team).
2. The page shows three phones: **Patient**, **Health worker (CHW)** and **Clinic front desk** (Demo Baraka Clinic).
3. Click **Create referral R-0142**. The patient phone should receive:
   > Hi! You were referred to Demo Baraka Clinic for lab. Is anything stopping you from going? Reply here.
4. Type the messages below into the **Patient** phone, one at a time and in this order. None of them contain clinical keywords or a bare "yes" or "ok", so each one goes to Claude. The reply shows which label Claude chose.

| # | Type this as the patient | Claude's expected label | Expected reply |
|---|---|---|---|
| 1 | `went there yesterday the gate was locked, nobody there` | `clinic_closed` | On a weekday: "Demo Baraka Clinic is open Mon-Fri 8-4. Reply here if you still can't get seen." On a weekend: "Closed today. Try …", naming another clinic that is open |
| 2 | `how do i get there? i dont know the way` | `transport` | "To reach Demo Baraka Clinic: Matatu 33 from CBD. Address: Embakasi Rd, Embakasi." |
| 3 | `sielewi kiingereza, naomba kiswahili` | `language` | "Umetumwa Demo Baraka Clinic kwa lab. Saa za kazi: Mon-Fri 8-4." |
| 4 | `ok i can go monday` | `plan_ack` with a date | Patient: "Thanks! See you at Demo Baraka Clinic on <next Monday>." The **Clinic** phone also receives "R-0142 arriving <date>. Reply Y R-0142 when seen." |
| 5 | `my stomach has felt really strange since yesterday` | `clinical_symptom`, caught by Claude because no keyword matches | Patient: "A health worker will contact you now. If severe, go to Demo Faraja County Hospital or call +254700000104." The **CHW** phone receives "CLINICAL R-0142 …" |

The header line under the buttons shows the case status. It should move from `contacted` to `barrier_found`/`action_taken` and then `visit_scheduled`, and after message 5 it should show `escalation: clinical`.

**How to tell whether Claude is working**

| What you see after a free-text message | What it means |
|---|---|
| A specific reply such as clinic hours, directions or the Swahili message | Claude classified the message correctly |
| "Sorry, I didn't get that. What is stopping you…" | Claude answered but was not confident enough, so the agent asked again |
| "Thanks, a health worker will follow up." **and** a `CLINICAL R-0142` message on the CHW phone, for a message that is not about health | **The Claude call failed** (missing or invalid `ANTHROPIC_API_KEY`, timeout or outage). Check the key and logs in Render |

These replies come from rules, not from Claude, so they don't tell you whether Claude works:

- A bare `yes`, `no` or `ok`.
- A message containing a clinical keyword, such as `my chest hurts`.

**Continuing the flow after the escalation**

6. On the **CHW** phone, send `1 R-0142` to claim the case. Anything the CHW types next goes straight to the patient, and patient replies come back to the CHW.
7. On the CHW phone, send `DONE R-0142` to hand the case back to the agent.
8. Click **+1 day** or **+2 days** until the clock in the header passes the visit date. The follow-up goes out the day after the visit date. The patient is asked "Did you get seen at Demo Baraka Clinic? Reply YES or NO." The clinic is asked "Was R-0142 seen? Reply Y R-0142 or N R-0142."
9. On the **Clinic** phone, send `Y R-0142`. The case becomes `completed (clinic-confirmed)`, and the dashboard's completion rate updates.

**Note: the hosted demo has a single shared demo case.** R-0142 is stored on the server's disk and there is no reset button. If someone has already taken it to `completed` or `lost`, the patient phone gets no replies. If it is stuck in an escalation, the patient only gets the holding reply until a CHW claims the case (step 6) and finishes it. Run the app locally (Option B) for a fresh case.

### Option B: run it locally

Requires Python 3.11.

```bash
git clone https://github.com/oscar598/sms-voice.git
cd sms-voice
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Set up the `.env` file in the project root. It is gitignored. If yours has none, create one; the variables it needs are in the Configuration table below. The local `.env` already sets simulator mode, port 5055 and `sim.db`. You only need to fill in:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is read by `python app.py`, `python eval.py` and `python try_claude.py`. Tests never read it. Variables already set in your shell take priority over the file.

Start the app:

```bash
python app.py
```

Then open:

- http://localhost:5055/ for the home page
- http://localhost:5055/sim for the simulator (no password locally unless you set `SIM_PASSWORD`). Follow the test steps above.
- http://localhost:5055/api/dashboard for the raw dashboard data as JSON

To fill the dashboard with the 40 synthetic demo cases, stop the app and run `python seed.py --db sim.db`, or start the app with `SEED_DEMO=1` on an empty database. To start over with a fresh R-0142, stop the app and delete `sim.db`.

**Connecting the Lovable dashboard to a local server.** Run [tunnel.sh](tunnel.sh) to expose port 5055 on a fixed ngrok domain, then point the dashboard at that URL.

### Live SMS over Twilio (optional)

1. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM`, and leave `SIM_MODE` unset.
2. Point the Twilio number's messaging webhook at `https://<public-host>/sms`.
3. Note that requests without a valid `X-Twilio-Signature` are rejected with 403, and that trial accounts can only text verified numbers.

### Test Claude from the command line

[try_claude.py](try_claude.py) sends one patient message to Claude and prints what the agent would do. It uses demo case R-0142 and does not need the server or dashboard. Nothing is saved and no SMS is sent.

```bash
python try_claude.py "went there yesterday the gate was locked, nobody there"
python try_claude.py          # interactive: type messages, empty line to quit
```

Example output:

```
Message:   ok i can go monday
Claude:    {"barrier": "plan_ack", "confidence": 0.9, "clinical_flag": false, ...}  (2.8s)
Decision:  plan_ack  (reason: ok)
Patient:   Thanks! See you at Demo Baraka Clinic on 2026-10-05.
Clinic:    R-0142 arriving 2026-10-05. Reply Y R-0142 when seen.
```

Unlike the live app, the script calls Claude even when a clinical keyword matches, so you always see the model's own label. When that happens it adds a line saying the live app would have skipped Claude. If the call fails, it prints `Claude: FAILED (...)` and shows the fallback handoff to a health worker.

### Tests and evaluation

```bash
pytest                        # unit tests; no API key or network needed
python eval.py tuning         # run the tuning set through Claude and the rules
python eval.py tuning --limit 10
python eval.py heldout        # needs eval/heldout.jsonl, written by someone who did not tune the prompt
```

### Configuration

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for the classifier |
| `CLASSIFY_MODEL` | Model ID (default `claude-opus-5-5`) |
| `CLASSIFY_TIMEOUT` | Classifier timeout in seconds (default 6) |
| `TWILIO_AUTH_TOKEN` | Required. Any value in `SIM_MODE` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_FROM` | Needed for live SMS only |
| `SIM_MODE` | `1` enables `/sim` and keeps all SMS on screen |
| `SIM_PASSWORD` / `REQUIRE_SIM_PASSWORD` | Password for `/sim` / refuse to start without one |
| `SEED_DEMO` | `1` loads the demo cases on first start with an empty database |
| `DB_PATH`, `PORT` | SQLite file (default `referrals.db`) and HTTP port (default 5000) |
| `REFERRAL_TOKEN` | Bearer token for `POST /api/referrals`. If unset, the endpoint is off |
| `DASHBOARD_ORIGIN` | Comma-separated CORS origins allowed to call `/api/*` |

### Deploying

[render.yaml](render.yaml) defines one Render web service with a persistent disk. The service must run **one worker and one thread**, because case updates are not protected against concurrent writes.

## Pipeline roadmap

How one incoming SMS moves through the code:

```
 Incoming SMS (Twilio /sms or simulator /sim)               app.py
        │
        ▼
 Who sent it? (looked up by phone number)                   app.py
        ├── CHW     ─► command: 1 / DONE / LOST R-0142, other text is relayed to the patient
        ├── Clinic  ─► reply:   Y / N R-0142 (seen or not seen)
        └── Patient ─► ROUTER, checked in this order:       core.route()
              1. "transport"
              2. "cost"
              3. "wrong_facuility"
              4. "missing_documents"
              5. "scheduling"
              6. "clinic_closed"
              7. "turned_away"
              8. "language"
              9. "fear_confusion"
              10. "clinical_sympton"
              11. "plan_ack"
              12. "unknown"

                        │
                        ▼
 Claude labels the message (JSON only)                      classify.py
        │
        ▼
 Guards: invalid output, API error, low confidence          core.apply_guards()
         or several barriers ─► "unknown"
        │
        ▼
 Rules table picks exactly one action                       core.decide()
        │
        ▼
 Send approved templates to patient / clinic / CHW,         app.py, templates.py
 log every barrier and action in the events table           db.py
        │
        ▼
 Timers: reminders, follow-up after the visit date,         scheduler.py
         completion, lost after 7 quiet days
        │
        ▼
 Dashboard and clinic reliability radar read the events     metrics.py
```

### Classification of a patient's response

Claude picks one label from this fixed list. The code, not Claude, then decides the action and the reply.

| Label | Meaning | Example message | What the agent does |
|---|---|---|---|
| `transport` | Can't get there | "how do i get there? i dont know the way" | Sends the clinic's directions and address |
| `cost` | Worried about fees | "how much will the lab cost" | Sends the clinic's cost note, or hands to a CHW if it has none |
| `wrong_facility` | The clinic doesn't offer the service | "they said they don't do this test here" | Points the case to another clinic that does and tells the patient |
| `missing_documents` | Lacks a required document | "i dont have my ID" | Sends the list of documents to bring. A missing referral letter goes to a CHW |
| `scheduling` | Can't get or doesn't know the appointment | "when can i come? i work all week" | Sends booking instructions. If a date is given, schedules the visit and tells the clinic |
| `clinic_closed` | Went and it was shut | "went there yesterday the gate was locked" | Sends posted hours, or another clinic if it's closed today. Counts toward the radar |
| `turned_away` | Open, but sent home | "they told me come back monday" | Tells the clinic the patient is coming on that date. Without a date, goes to a CHW. Counts toward the radar |
| `language` | Didn't understand the language | "sielewi kiingereza, naomba kiswahili" | Resends in Swahili. Other languages go to a CHW |
| `fear_confusion` | Scared or unsure why they were referred | "why do i need to go, is it serious?" | Sends a plain explanation of the referral |
| `clinical_symptom` | Mentions any symptom or feeling worse | "my stomach has felt really strange" | Sends safety instructions with the emergency facility and number. Pages a CHW urgently |
| `plan_ack` | No barrier, just a plan or thanks | "ok i can go monday" | Thanks the patient. If a date is given, schedules the visit and tells the clinic |
| `unknown` | Unclear, off-topic or several barriers | "hmm maybe" | Asks once more, then hands to a CHW |

Safety rules applied after Claude answers:

- If Claude flags any symptom (`clinical_flag`), the message is treated as `clinical_symptom`, whatever the label.
- Confidence below 0.7, or more than one barrier in one message, becomes `unknown`.
- An API error, timeout or invalid response becomes `unknown` and goes to a CHW as **urgent**, so an outage can never delay a possible clinical message.
- Extra details Claude extracts, such as a return date, area or missing document, are checked before use and dropped if invalid. For example, a date must fall within the next 30 days.

## Future steps

- **Demo reset.** Add a reset button or endpoint for R-0142 so each tester on the hosted demo starts fresh.
- **Real SMS pilot.** Register a Twilio number for sending to real phones and move off the trial account. Agree the consent and opt-in wording for the first message.
- **Held-out evaluation.** Have a teammate who did not tune the prompt write `eval/heldout.jsonl`, with at least 15 clinical messages. Report clinical recall and the count of wrong actions from it.
- **CHW baton.** Rotate offers across several CHWs with timeouts, fall back to a supervisor, and handle claimed cases that are never closed.
- **Open design decisions.**
  - Add a `HELP` reply.
  - Add the "reply 2 to talk to someone" route.
  - Choose when actions deferred on mixed clinical messages are released.
  - Define the radar's denominator and time window.
- **Shared phones.** Support more than one open referral on the same patient phone.
- **Safe scaling.** Add guarded case updates so the service can run more than one worker. Replace the global fast-forward clock with a per-case one.
- **Languages and voice.** Expand the Swahili and Sheng templates and clinical keywords. Add a scripted outbound voice call for patients who cannot read SMS.
- **Evidence for the setting.** Source referral-completion data for the target locale before making any quantitative claims.

All facilities, phone numbers and cases in this project are synthetic. No real patient data is used.
