"""Background worker. Two jobs, both pulled from the leads table:
1. CALL: take a queued lead, respect legal calling hours, 'call' it.
2. SYNC: push the call outcome to the client's CRM, retrying with backoff when the CRM fails.
"""
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from db import SKIP_LOCKED, init_db, iso, now_utc, run_sql

IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving, so a fixed offset is exact
CALL_START, CALL_END = 9, 21  # TRAI: promotional calls only between 9:00 and 21:00

# Default: this app's own fake CRM. PORT is set by Render (and other hosts); 8000 is uvicorn's local default.
CRM_URL = os.environ.get("CRM_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/mock-crm"
MAX_SYNC_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2  # waits 2, 4, 8, 16s. shortcut: seconds so the demo is watchable; use minutes in production.

log = logging.getLogger("worker")


# ---------- shared: claim one due lead ----------

def claim(now: datetime, ready: tuple[str, ...], working: str) -> dict | None:
    """Atomically grab one due lead in a `ready` status and move it to `working`,
    so no other worker can grab the same one."""
    marks = ", ".join("?" * len(ready))
    rows = run_sql(f"""
        UPDATE leads SET status = ?
        WHERE id = (SELECT id FROM leads
                    WHERE status IN ({marks}) AND next_attempt_at <= ?
                    ORDER BY id LIMIT 1{SKIP_LOCKED})
        RETURNING *
    """, (working, *ready, iso(now)))
    return rows[0] if rows else None


# ---------- job 1: calling ----------

def in_calling_window(now: datetime) -> bool:
    return CALL_START <= now.astimezone(IST).hour < CALL_END


def next_window_start(now: datetime) -> datetime:
    """Next 9:00 IST. Only used outside the window: before 9 -> today, after 21 -> tomorrow."""
    local = now.astimezone(IST)
    start = local.replace(hour=CALL_START, minute=0, second=0, microsecond=0)
    if local.hour >= CALL_START:
        start += timedelta(days=1)
    return start


def mock_call(lead: dict) -> str:
    # shortcut: stands in for Huvo's AI voice agent.
    time.sleep(2)
    return random.choice(["interested", "site_visit_booked", "not_interested", "no_answer"])


def handle_call(lead: dict, now: datetime, call) -> None:
    if not in_calling_window(now):
        wake = next_window_start(now)
        run_sql("UPDATE leads SET status = 'scheduled', next_attempt_at = ? WHERE id = ?", (iso(wake), lead["id"]))
        log.info("lead %s held until %s IST", lead["id"], wake.strftime("%d %b %H:%M"))
        return

    # shortcut: if the call crashes, the lead stays 'calling'; a stuck-lead sweeper would reset it.
    outcome = call(lead)
    # next_attempt_at = now -> the outcome is immediately due for CRM sync.
    run_sql("UPDATE leads SET status = 'called', outcome = ?, next_attempt_at = ? WHERE id = ?",
            (outcome, iso(now), lead["id"]))
    log.info("lead %s called: %s", lead["id"], outcome)


# ---------- job 2: CRM sync with retries ----------

def push_to_crm(lead: dict) -> None:
    """Raises on any failure: network error, timeout, or non-2xx response."""
    r = httpx.post(
        CRM_URL,
        json={"external_id": lead["id"], "name": lead["name"], "phone": lead["phone"],
              "campaign": lead["campaign"], "outcome": lead["outcome"]},
        # Same key on every retry: if the CRM saved it but the reply got lost, it can ignore the repeat.
        headers={"Idempotency-Key": f"lead-{lead['id']}"},
        timeout=5,
    )
    r.raise_for_status()


def handle_sync(lead: dict, now: datetime, push) -> None:
    try:
        push(lead)
    except Exception as e:
        attempts = lead["sync_attempts"] + 1
        error = f"{type(e).__name__}: {e}"[:300]
        if attempts >= MAX_SYNC_ATTEMPTS:
            # Dead letter: stop retrying, keep the lead and the reason so a human can look and replay it.
            run_sql("UPDATE leads SET status = 'failed', sync_attempts = ?, last_error = ? WHERE id = ?",
                    (attempts, error, lead["id"]))
            log.error("lead %s CRM sync FAILED after %s attempts: %s", lead["id"], attempts, error)
            return
        wait = BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)  # exponential backoff: 2, 4, 8, 16s
        run_sql("UPDATE leads SET status = 'sync_retry', sync_attempts = ?, last_error = ?, next_attempt_at = ? "
                "WHERE id = ?", (attempts, error, iso(now + timedelta(seconds=wait)), lead["id"]))
        log.warning("lead %s CRM sync attempt %s failed, retry in %ss: %s", lead["id"], attempts, wait, error)
        return

    run_sql("UPDATE leads SET status = 'synced', last_error = NULL WHERE id = ?", (lead["id"],))
    log.info("lead %s synced to CRM", lead["id"])


# ---------- the loop ----------

def process_one(now: datetime | None = None, call=mock_call, push=push_to_crm) -> bool:
    """Handle one lead (calls first: speed-to-lead matters most). Returns False when nothing was due."""
    now = now or now_utc()
    if lead := claim(now, ("queued", "scheduled"), "calling"):
        handle_call(lead, now, call)
        return True
    if lead := claim(now, ("called", "sync_retry"), "syncing"):
        handle_sync(lead, now, push)
        return True
    return False


def run_forever():
    while True:
        try:
            if not process_one():
                time.sleep(1)  # nothing due; don't spin the CPU
        except Exception:
            log.exception("worker error")  # never let one bad lead kill the worker
            time.sleep(1)


if __name__ == "__main__":  # run as its own process: python worker.py
    logging.basicConfig(level=logging.INFO)
    init_db()
    run_forever()
