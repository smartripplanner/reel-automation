"""
Job Service — in-memory job tracking for async reel generation.

Each job has:
  id        — UUID string
  status    — "queued" | "running" | "completed" | "failed"
  logs      — list of log message strings (appended during pipeline)
  result    — pipeline output dict (set on completion)
  created_at — ISO timestamp

Jobs live for the process lifetime. A simple TTL cleanup runs on creation
to drop jobs older than 2 hours to prevent unbounded growth.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# In-memory store
# ─────────────────────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}

_JOB_TTL_HOURS = 2


def _cleanup_old_jobs() -> None:
    cutoff = datetime.utcnow() - timedelta(hours=_JOB_TTL_HOURS)
    stale = [
        jid for jid, job in _jobs.items()
        if datetime.fromisoformat(job["created_at"]) < cutoff
    ]
    for jid in stale:
        _jobs.pop(jid, None)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────

def create_job() -> str:
    """Create a new queued job and return its ID."""
    _cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "logs": [],
        "result": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    return job_id


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def append_log(job_id: str, message: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["logs"].append(message)


def set_running(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["status"] = "running"


def set_completed(job_id: str, result: dict) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["status"] = "completed"
        job["result"] = result


def set_failed(job_id: str, error: str) -> None:
    job = _jobs.get(job_id)
    if job is not None:
        job["status"] = "failed"
        job["result"] = {"error": error}
