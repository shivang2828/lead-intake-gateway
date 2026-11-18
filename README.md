# Lead Intake Gateway

A small backend that sits between lead sources (Meta ads, property portals, website forms) and an AI calling
agent. It makes sure every lead is **clean, called once, called at a legal hour, and lands in the CRM**,
even when the CRM is down.

Built as a demo for [Huvo AI](https://huvo.ai): speed-to-lead only works if the plumbing behind it is reliable.

**Live demo:** _[add your Render URL]_ · **90s walkthrough:** _[add your Loom link]_

## What it handles

| Problem | What the gateway does |
|---|---|
| Same buyer typed as `98765 43210`, `+91-98765-43210`, `09876543210` | Normalises every phone to E.164 (`+919876543210`) |
| Same lead arrives twice (portal retries, buyer re-submits) | Unique index on `(campaign, phone)`; a repeat returns `200 duplicate` with the original id (idempotent) |
| Lead comes in at 11:30 pm | Not called. Scheduled for 9:00 IST (TRAI calling window 9:00–21:00) |
| Several workers running at once | Atomic claim with `FOR UPDATE SKIP LOCKED`: a lead is never called twice |
| CRM returns 503 / times out | Retries with exponential backoff (2, 4, 8, 16s) and the same `Idempotency-Key` |
| CRM keeps failing | After 5 attempts the lead goes to a dead letter with the error; one click replays it |

## How it works

```
 lead source ──POST /leads──▶  API  ── validate, normalise, dedupe ──▶  leads table (Postgres)
                               (fast reply, no slow work)                   │  status + next_attempt_at
                                                                            ▼
                                                   worker ── claim one due lead (SKIP LOCKED)
                                                     │  9:00–21:00 IST? ── no ──▶ scheduled for 9:00
                                                     ▼ yes
                                                  AI call (mocked) ──▶ outcome
                                                     ▼
                                                  push to CRM ── fail ──▶ retry with backoff ──▶ dead letter after 5
                                                     ▼ ok
                                                   synced
```

The table is the queue: `status` + `next_attempt_at` decide what is due, so call windows and retry backoff
use the same mechanism, and nothing is lost if the process restarts.

## Proof it's safe with concurrent workers

`test_four_workers_at_once_never_call_anyone_twice` runs 4 worker threads over 30 leads against real
Postgres and asserts every lead is called exactly once. With `FOR UPDATE SKIP LOCKED` removed, the same
test fails every run, because leads get called twice.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
Open http://localhost:8000 for the live dashboard ("Send 6 test leads" shows dedupe, calls and CRM retries),
or http://localhost:8000/docs for the API. Without `DATABASE_URL` it uses a local SQLite file, so no setup is needed.

```bash
pytest                                      # 25 tests on SQLite
DATABASE_URL=postgresql://... pytest        # same tests on Postgres
```

## API

| Method | Path | |
|---|---|---|
| `POST` | `/leads` | `{name, phone, source, campaign}` → `201 queued` or `200 duplicate` |
| `GET` | `/leads` | All leads with status, call outcome, CRM attempts, last error |
| `POST` | `/leads/{id}/retry` | Replay a dead-lettered lead |
| `POST` | `/mock-crm` | Fake CRM that fails `CRM_FAIL_RATE` of the time (default 50%) |

Config: `DATABASE_URL` (Postgres; SQLite if unset), `CRM_URL` (defaults to the built-in mock), `CRM_FAIL_RATE`.

## Stack

Python 3.11 · FastAPI · PostgreSQL (psycopg 3 + connection pool) · plain HTML dashboard · pytest · Render

## What I'd do next

- Don't retry 4xx CRM errors (they won't succeed); add jitter to backoff
- Sweeper for leads stuck in `calling`/`syncing` after a crash
- Per-campaign rate limit so a big launch doesn't exhaust telephony lines
- Auth + multi-tenancy (per-developer access), pagination on `/leads`
- Run workers as separate processes (`python worker.py`); the claim already makes that safe
