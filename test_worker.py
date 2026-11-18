from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import worker
from db import run_sql
from main import app
from worker import IST, MAX_SYNC_ATTEMPTS, next_window_start, process_one

client = TestClient(app)


def crm_ok(lead):
    pass


class FlakyCRM:
    """Fails the first `failures` pushes, then works."""
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def __call__(self, lead):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("CRM down")


def ist(day, hour, minute=0):
    # A future date, so leads created "now" (real clock) are always due in these tests.
    return datetime(2099, 11, day, hour, minute, tzinfo=IST)


def add_lead(phone="9876543210"):
    return client.post("/leads", json={"name": "Test", "phone": phone}).json()["id"]


def get_lead(lead_id):
    return run_sql("SELECT * FROM leads WHERE id = ?", (lead_id,))[0]


def test_next_window_start():
    assert next_window_start(ist(18, 7, 30)) == ist(18, 9)   # early morning -> today 9am
    assert next_window_start(ist(18, 21, 0)) == ist(19, 9)   # 9pm sharp -> tomorrow
    assert next_window_start(ist(18, 23, 45)) == ist(19, 9)  # late night -> tomorrow


def test_daytime_lead_gets_called_then_synced():
    lead_id = add_lead()
    assert process_one(now=ist(18, 11), call=lambda lead: "interested") is True
    lead = get_lead(lead_id)
    assert (lead["status"], lead["outcome"]) == ("called", "interested")

    process_one(now=ist(18, 11), push=crm_ok)
    assert get_lead(lead_id)["status"] == "synced"
    assert process_one(now=ist(18, 11)) is False  # nothing left to do


def test_night_lead_waits_until_9am():
    lead_id = add_lead()
    process_one(now=ist(18, 22, 30), call=lambda lead: "should not be called")
    lead = get_lead(lead_id)
    assert lead["status"] == "scheduled"
    assert lead["outcome"] is None

    assert process_one(now=ist(19, 8, 59)) is False             # still too early: not touched
    process_one(now=ist(19, 9, 1), call=lambda lead: "no_answer")  # window opens: now it's called
    assert get_lead(lead_id)["status"] == "called"


def test_a_lead_is_claimed_only_once():
    add_lead()
    first = worker.claim(ist(18, 11), ("queued",), "calling")
    second = worker.claim(ist(18, 11), ("queued",), "calling")  # a second worker asking at the same time
    assert first is not None and second is None


def test_four_workers_at_once_never_call_anyone_twice():
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    for i in range(30):
        add_lead(phone=f"98{i:08d}")
    calls = Counter()

    def fake_call(lead):
        calls[lead["id"]] += 1
        return "interested"

    def worker_loop():
        while process_one(now=ist(18, 11), call=fake_call, push=crm_ok):
            pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in [pool.submit(worker_loop) for _ in range(4)]:
            f.result()  # re-raises any error from a worker thread

    assert len(calls) == 30 and set(calls.values()) == {1}  # all 30 called, each exactly once
    assert run_sql("SELECT COUNT(*) AS n FROM leads WHERE status = 'synced'")[0]["n"] == 30


def test_crm_failure_retries_with_backoff_then_succeeds():
    lead_id = add_lead()
    t = ist(18, 11)
    process_one(now=t, call=lambda lead: "interested")
    crm = FlakyCRM(failures=2)

    process_one(now=t, push=crm)                         # attempt 1 fails
    lead = get_lead(lead_id)
    assert (lead["status"], lead["sync_attempts"]) == ("sync_retry", 1)
    assert "CRM down" in lead["last_error"]

    assert process_one(now=t + timedelta(seconds=1), push=crm) is False  # backoff: not due yet (waits 2s)
    process_one(now=t + timedelta(seconds=2), push=crm)  # attempt 2 fails -> now waits 4s
    assert process_one(now=t + timedelta(seconds=5), push=crm) is False
    process_one(now=t + timedelta(seconds=6), push=crm)  # attempt 3 succeeds

    lead = get_lead(lead_id)
    assert (lead["status"], lead["last_error"], crm.calls) == ("synced", None, 3)


def test_crm_always_down_goes_to_dead_letter_and_can_be_replayed():
    lead_id = add_lead()
    t = ist(18, 11)
    process_one(now=t, call=lambda lead: "interested")
    crm = FlakyCRM(failures=999)
    for _ in range(20):                                  # plenty of time: step 1 minute each
        t += timedelta(minutes=1)
        process_one(now=t, push=crm)

    lead = get_lead(lead_id)
    assert (lead["status"], lead["sync_attempts"]) == ("failed", MAX_SYNC_ATTEMPTS)
    assert crm.calls == MAX_SYNC_ATTEMPTS                # stopped trying: no endless retries

    assert client.post(f"/leads/{lead_id}/retry").json()["status"] == "sync_retry"
    process_one(push=crm_ok)                             # CRM fixed -> replay works
    assert get_lead(lead_id)["status"] == "synced"
    assert client.post(f"/leads/{lead_id}/retry").status_code == 404  # only failed leads can be replayed
