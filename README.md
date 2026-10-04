# SMS Voice: referral closure agent

A referral gets written, and then often nothing happens. A number of outpatient specialty referrals are never completed, and nobody owns the gap between "referred" and "seen".

This project is a low-bandwidth SMS agent that closes that gap. It works on a plain feature phone with no app. For each referred patient it:

1. texts the patient and finds out **why** the referral is stuck (transport, cost, clinic closed, turned away, and so on),
2. takes the **smallest permitted administrative action**, such as sending directions, a document checklist or posted hours, or telling the clinic the patient is coming,
3. escalates to a community health worker (CHW) when needed, and
4. **confirms the visit happened**, preferably with the clinic.

It never diagnoses and never gives clinical advice. Any clinical mention escalates to a human.

**Clinic Reliability Radar.** Barriers reported by patients are added up per clinic, so the dashboard can flag a facility, for example "Clinic B: 6 of 11 patients found it closed or were turned away". This is shown as a signal that needs follow-up, not as a score, because it rests on what patients report.

The full design and review decisions are in [docs/designs/referral-agent.md](docs/designs/referral-agent.md).

## How it works

**The LLM classifies. Code decides.**

```
SMS ─► /sms (Twilio, signature checked) ─┐
/sim (simulated phones) ─────────────────┴─► router (deterministic)
    0. clinical keyword pre-filter (always first for patients)
    1. STOP / START
    2. CHW commands   ("1 R-0142", "DONE R-0142", "LOST R-0142")
    3. clinic replies ("Y R-0142", "N R-0142")
    4. exact yes/no replies to an open prompt
    5. free text ─► Claude classifier ─► guards ─► rules table ─► actions
                                                        │
                    patient template · clinic SMS · CHW baton · events log
```

- The classifier returns a label from a fixed list: `transport`, `cost`, `wrong_facility`, `missing_documents`, `scheduling`, `clinic_closed`, `turned_away`, `language`, `fear_confusion`, `clinical_symptom`, `plan_ack` or `unknown`.
- Low confidence, several barriers in one message, or invalid output all become `unknown`, which goes to a CHW. An API error or timeout follows the clinical escalation path.
- Patients only ever receive pre-written templates with filled-in slots, never text the model wrote.
- Every barrier and action is logged to an append-only `events` table. All dashboard metrics come from that table.
- After `STOP` the patient receives nothing at all until they send `START`.

## Project layout

| File | Purpose |
|---|---|
| [app.py](app.py) | Flask app: `/sms` webhook, `/sim` pages, `/api/*`, referral form, outbound sending |
| [core.py](core.py) | Pure logic: router, clinical pre-filter, field validation, rules table |
| [classify.py](classify.py) | Claude call with JSON/enum validation |
| [scheduler.py](scheduler.py) | Reminders, follow-ups and timeouts, run on each request |
| [db.py](db.py) | SQLite schema and queries |
| [templates.py](templates.py) | Every SMS the system can send |
| [metrics.py](metrics.py) | Dashboard numbers and the reliability radar |
| [seed.py](seed.py) | Synthetic facilities, CHWs and 40 demo cases (Nairobi locale) |
| [eval.py](eval.py), [eval/](eval/) | Labelled classifier evaluation |
| [home.html](home.html), [referral.html](referral.html), [sim.html](sim.html) | Home page, new-referral form, simulated phones |
| [render.yaml](render.yaml) | Render deploy blueprint |
| [tunnel.sh](tunnel.sh) | ngrok tunnel on a fixed domain, restarted automatically |

## Running locally

Requires Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root. It is gitignored, and `app.py` and `eval.py` load it on start:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### Simulator mode (no Twilio needed)

```bash
SIM_MODE=1 TWILIO_AUTH_TOKEN=sim-only DB_PATH=sim.db PORT=5055 python app.py
```

On Windows PowerShell, set each variable with `$env:NAME = "value"` first.

Open http://localhost:5055/sim. It shows three phones (patient, CHW and clinic) that feed the same router as real SMS. Outgoing messages appear on screen instead of being sent. Fast-forward moves the clock so follow-ups come due.

To load the 40 synthetic demo cases into the dashboard:

```bash
python seed.py --db sim.db
```

### Live SMS over Twilio

Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM`, leave `SIM_MODE` unset, and point the Twilio number's messaging webhook at `https://<your-public-host>/sms`. [tunnel.sh](tunnel.sh) provides a stable ngrok URL for local runs. Requests without a valid `X-Twilio-Signature` are rejected with 403.

Twilio trial accounts can only text verified numbers and add a prefix to every message, which is why patient templates are kept to 120 characters or fewer.

## Configuration

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for the classifier |
| `CLASSIFY_MODEL` | Model ID (default `claude-opus-5-5`) |
| `CLASSIFY_TIMEOUT` | Classifier timeout in seconds (default 6) |
| `TWILIO_AUTH_TOKEN` | Required. Validates webhook signatures. Any value works in `SIM_MODE` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_FROM` | Required for live SMS |
| `SIM_MODE` | `1` enables `/sim` and keeps all outbound SMS on screen |
| `SIM_PASSWORD` | HTTP Basic password protecting `/sim` |
| `REQUIRE_SIM_PASSWORD` | `1` refuses to start with an unprotected `/sim` |
| `SEED_DEMO` | `1` loads the demo cases on first start when the database is empty |
| `DB_PATH` | SQLite file (default `referrals.db`) |
| `PORT` | HTTP port (default 5000) |
| `REFERRAL_TOKEN` | Bearer token for `POST /api/referrals`. If unset, the endpoint is off |
| `DASHBOARD_ORIGIN` | Comma-separated CORS origins for `/api/*`. `*` wildcards are allowed |
| `DASHBOARD_URL`, `REPO_URL` | Links shown on the home page |

## API

| Route | Description |
|---|---|
| `GET /` | Home page |
| `GET /referrals/new` | New-referral form |
| `POST /sms` | Twilio inbound webhook |
| `GET /api/dashboard` | Completion metrics, top barriers and the reliability radar |
| `GET /api/state` | Unreachable patients (failed sends) |
| `GET /api/facilities` | Facility list |
| `POST /api/referrals` | Create a referral and text the patient. Needs `Authorization: Bearer $REFERRAL_TOKEN` |
| `/sim`, `/sim/*` | Simulator, available only in `SIM_MODE` |

The dashboard UI is hosted separately on Lovable and reads these endpoints. See [docs/lovable-dashboard-prompt.md](docs/lovable-dashboard-prompt.md).

## Tests and evaluation

```bash
pytest                       # unit tests; no API key or network needed
python eval.py tuning        # 100 tuning messages, calls Claude
python eval.py heldout       # held-out set, which gives the headline numbers
python eval.py tuning --limit 10
```

The held-out set (`eval/heldout.jsonl`, format in [eval/heldout.TEMPLATE.jsonl](eval/heldout.TEMPLATE.jsonl)) is written by someone who does not tune the prompt. It must include at least 15 clinical messages. The targets are:

- 100% recall on the clinical messages,
- at most one wrong action, reported as k/N,
- at least 80% barrier accuracy, with "safe `unknown`" answers reported separately.

## Deploying

[render.yaml](render.yaml) defines one Render web service with a persistent disk. It runs in simulator mode, seeds the demo data on first start, and requires a `/sim` password. Enter the secrets in the Render dashboard.

The service must run **one worker with one thread**. Case updates are not protected against concurrent writes, which is a deliberate hackathon shortcut. Add guarded updates before scaling up.

## Known limitations

- One open case per patient phone. A shared phone with two referrals is not handled.
- Fast-forward moves one global clock, so the timers of every case fire together.
- Stuck cases stay stuck by design. A patient on a paused case gets a holding reply.
- Still undefined: the `HELP` reply, the "reply 2 to talk to someone" route, and the radar's denominator and time window.

All facilities, phone numbers and cases are synthetic. No real patient data is used.
