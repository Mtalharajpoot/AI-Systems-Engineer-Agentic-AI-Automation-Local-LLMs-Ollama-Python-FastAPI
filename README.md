<p align="center">
  <img src="docs/banner.png" alt="24/7 Maintenance Triage Agent" width="100%">
</p>

<h1 align="center">24/7 Maintenance Triage Agent</h1>
<p align="center">
  A small, fully-tested prototype of an agentic system that triages property-maintenance requests:<br>
  deterministic rules first, a local open-source LLM only when rules can't decide, and a human in the loop for anything risky.
</p>

<p align="center">
  <a href="#tests"><img alt="tests" src="https://img.shields.io/badge/tests-23%20passing-brightgreen"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-lightgrey">
  <img alt="status" src="https://img.shields.io/badge/status-prototype-orange">
</p>

---

## The scenario

> A tenant emails at 11:30 PM: *"There is water coming through the ceiling. I tried calling and nobody answered."*
> Nobody is monitoring the inbox. What should happen?

This repo is a working answer to that question, built around one idea:

> **The best AI architecture sometimes uses less AI.**

```
Event -> State -> Deterministic rules -> Local LLM only if needed -> Action -> Validation -> State update -> Next action
```

Most tenant messages are obvious ("water pouring from the ceiling" = emergency; "faucet keeps dripping" = routine).
Those are classified by plain regex rules — instant, free, and unit-tested. A local LLM (via
[Ollama](https://ollama.com)) is only consulted for the messages rules can't confidently classify, and even then
it only returns a classification — it never sends a message or dispatches a vendor directly.

## What happens to a message

```
webhook (n8n / Twilio / email)  ->  POST /inbound
  1. de-duplicate by message id            webhooks retry; this must not create a second ticket
  2. identify the tenant                   unknown sender -> human, never auto-reply
  3. open ticket already?                  yes -> follow-up (update memory, escalate only if worse)
  4. rules.classify()                      regex rules: emergency / urgent / routine, zero cost
       no rule matched -> local LLM        schema-constrained JSON, Pydantic-validated, 1 retry
       LLM down / invalid / unsure         -> HUMAN (never a silent guess)
  5. act                                   fixed templates, retrieve property facts (e.g. water shut-off),
                                            dispatch a vendor, alert the on-call human for emergencies
  6. state machine validates the change    illegal transitions raise; every step is an audit-log event
  7. outbox flush AFTER commit             failed deliveries are kept and retried

background sweeper (every 10s)
  vendor silent past the deadline -> next vendor -> out of vendors/attempts -> human
```

## Why it's built this way

| Design choice | File | What it's for |
|---|---|---|
| Regex rules run before any model | `app/rules.py` | Most messages are obvious; cheap, instant, and testable |
| Emergency rules over-escalate on purpose | `app/rules.py` | A false alarm costs a phone call; a missed flood costs a building |
| A chirping smoke detector isn't a fire, but "beeping **and** I see smoke" still is | `app/rules.py` | Guard false positives without hiding a real emergency |
| The LLM only classifies; it never sends messages or writes safety text | `app/llm.py`, `app/templates.py` | Model output is untrusted input; safety-critical wording is fixed |
| Schema-constrained JSON + Pydantic validation + 1 retry + human fallback | `app/llm.py` | Reliability and graceful failure recovery |
| Model sees unit facts + a 400-char rolling summary + the one new message | `app/db.py`, `app/llm.py` | Token efficiency — never resend full histories |
| Explicit ticket state machine | `app/db.py` (`TRANSITIONS`) | Illegal state changes are impossible, not just discouraged |
| Idempotent webhook (unique message id) | `app/agent.py` | Retries can't create duplicate tickets or duplicate alerts |
| Outbox pattern: write in the same transaction, send after commit | `app/notify.py` | State and messages can never disagree; failed sends are retried |
| Vendor timeout -> backup -> human | `app/agent.py` (`sweep`) | Follow-through to resolution, not just ticket creation |
| Append-only event log + `/stats` | `app/db.py`, `app/agent.py` | Observability — shows what % of messages needed *no* LLM at all |

## What's real vs. simulated

This is a prototype built to demonstrate the architecture, not a production system.

| Real | Simulated here (production equivalent) |
|---|---|
| Rules engine, state machine, idempotency, outbox, sweeper, retries, audit log | Tenant/vendor SMS & email (`outbox` table + console -> Twilio in production) |
| Local LLM call to Ollama with schema-constrained output | Property-management work orders (`work_orders` table -> Rentvine-style API) |
| Telegram alert to a human (optional, if configured) | Vendors, tenants, properties (seed data) |
| 23 automated tests | Workflow orchestration (n8n would sit in front of `/inbound` for webhook intake) |

## Quickstart

```bash
git clone https://github.com/<your-username>/maintenance-triage-agent.git
cd maintenance-triage-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest                     # 23 tests should pass
```

Run the guided scenario walkthrough without any LLM (fast, no setup):

```bash
LLM_MODE=mock python demo.py        # Windows PowerShell: $env:LLM_MODE="mock"; python demo.py
```

### Use a real local LLM

```bash
# 1. install Ollama: https://ollama.com
ollama pull qwen2.5:3b
# 2. unset LLM_MODE (or don't set it -- "ollama" is the default)
python demo.py 5                    # the one scenario that needs the LLM
```

No GPU on your machine? `docs/colab_demo.ipynb` runs the same code with a free Colab T4 GPU.

### Run the API

```bash
uvicorn app.main:app --reload
```
Open `http://127.0.0.1:8000/docs` and try `POST /inbound`, `GET /outbox`, `GET /tickets/{id}`, `GET /stats`.

## Tests

```bash
pytest -q
```
23 tests cover the rules engine, the full happy path, vendor-timeout escalation, idempotent webhook retries,
unknown senders, follow-up messages that get worse, and every LLM failure mode (down, invalid JSON, low
confidence, disabled).

## Known limitations

- Rules have no negation handling ("there is **no** smoke" still matches) and over-escalate by design.
- SQLite + an in-process sweeper thread: single-node only. Production should use Postgres and a real queue/worker.
- No auth/signature check on `/inbound` (production should verify Twilio/n8n webhook signatures).
- Business hours, time zones, and on-call rotations aren't modeled.
- No labeled evaluation set for the LLM classifier yet -- needed before trusting any model change.
- The confidence threshold (0.6) is a starting guess, not a tuned value.

## Project layout

```
app/rules.py      deterministic triage           app/llm.py       local LLM call, schema, retry
app/agent.py      the pipeline                   app/db.py        schema + state machine
app/notify.py     outbox + Telegram              app/pms.py       property-management sync (simulated)
app/templates.py  fixed message templates        app/main.py      FastAPI app + background sweeper
demo.py           scenario walkthrough           tests/           23 automated tests
docs/             banner + Colab notebook
```

## License

MIT -- see [LICENSE](LICENSE).
