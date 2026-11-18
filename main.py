"""Huvo Lead Intake Gateway - API. Leads are queued here; the background worker calls them and syncs to the CRM."""
import logging
import os
import random
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

import worker
from db import init_db, iso, now_utc, run_sql
from phones import normalize_indian_mobile


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    init_db()
    # shortcut: worker runs as a thread inside the API so the demo deploys as one service;
    # to scale, run `python worker.py` as separate processes (the atomic claim keeps them safe).
    threading.Thread(target=worker.run_forever, daemon=True).start()
    yield


app = FastAPI(title="Huvo Lead Intake Gateway", lifespan=lifespan)


class LeadIn(BaseModel):
    """What a lead source (Meta ad, property portal, website form) sends us."""
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=7, max_length=20)
    source: str = "website"
    campaign: str = "default"

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, v: str) -> str:
        return normalize_indian_mobile(v)  # a ValueError here becomes a 422 response


@app.post("/leads", status_code=201)
def create_lead(lead: LeadIn, response: Response):
    now = iso(now_utc())
    inserted = run_sql(
        """INSERT INTO leads (name, phone, source, campaign, next_attempt_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (campaign, phone) DO NOTHING
           RETURNING id""",
        (lead.name, lead.phone, lead.source, lead.campaign, now, now),
    )
    if inserted:
        # The API only saves and returns fast; the slow part (calling) is the worker's job.
        return {"id": inserted[0]["id"], "status": "queued"}

    # Already have this person for this campaign: answer calmly with the existing lead, don't error.
    # Lead sources retry on timeouts, so a repeat must be safe (idempotent).
    existing = run_sql("SELECT id FROM leads WHERE campaign = ? AND phone = ?", (lead.campaign, lead.phone))
    response.status_code = 200
    return {"id": existing[0]["id"], "status": "duplicate"}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html")


@app.get("/leads")
def list_leads():
    return run_sql("SELECT * FROM leads ORDER BY id DESC")


@app.post("/leads/{lead_id}/retry")
def retry_failed_lead(lead_id: int):
    """Replay a dead-lettered lead after the CRM problem is fixed."""
    rows = run_sql(
        "UPDATE leads SET status = 'sync_retry', sync_attempts = 0, next_attempt_at = ? "
        "WHERE id = ? AND status = 'failed' RETURNING id",
        (iso(now_utc()), lead_id),
    )
    if not rows:
        raise HTTPException(404, "no failed lead with that id")
    return {"id": lead_id, "status": "sync_retry"}


# ---- Fake CRM, only for the demo: behaves like a flaky third-party API ----
CRM_FAIL_RATE = float(os.environ.get("CRM_FAIL_RATE", "0.5"))


@app.post("/mock-crm")
def mock_crm(payload: dict):
    if random.random() < CRM_FAIL_RATE:
        raise HTTPException(503, "CRM temporarily unavailable")
    return {"ok": True, "received": payload.get("external_id")}
