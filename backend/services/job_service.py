"""
Job Service — hybrid in-memory + SQLite job tracking.

In-memory dict   → live logs and fast status reads during a running job.
SQLite (jobs table) → survives Render restarts; GET /jobs/{id} never 404s
                      for completed/failed jobs even after a deploy.

Lifecycle
─────────
  create_job()  → inserted in memory AND DB with status="queued"
  set_running() → updated in memory AND DB
  set_completed / set_failed → updated in memory AND DB

  get_job()     → memory first (full logs); DB fallback (no logs, status only)

On startup call recover_stale_jobs() in lifespan to mark any jobs that were
still "running" or "queued" when the previous process died as "failed".
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# In-memory store  (lives only for this process lifetime)
# ─────────────────────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_JOB_TTL_HOURS = 24  # keep live entries 24 h before evicting from memory


def _cleanup_old_jobs() -> None:
    cutoff = datetime.utcnow() - timedelta(hours=_JOB_TTL_HOURS)
    with _lock:
        stale = [
            jid for jid, job in _jobs.items()
            if datetime.fromisoformat(job["created_at"]) < cutoff
        ]
        for jid in stale:
            _jobs.pop(jid, None)


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers  (each call opens its own session — thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

def _db_write(job_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    """Insert-or-update the DB row for job_id.  Never raises — errors are logged."""
    try:
        from database import SessionLocal          # lazy — avoids import-time DB hit
        from models.job import Job

        db = SessionLocal()
        try:
            row = db.get(Job, job_id)
            if row is None:
                row = Job(
                    id=job_id,
                    status=status,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(row)
            else:
                row.status = status
                row.updated_at = datetime.utcnow()

            if result is not None:
                row.result_json = json.dumps(result)
            if error is not None:
                row.error_message = error

            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[JobService] DB write error (job={job_id}): {exc}")
        finally:
            db.close()
    except Exception as exc:
        print(f"[JobService] DB session error: {exc}")


def _db_read(job_id: str) -> dict | None:
    """Read a job record from DB.  Returns None if not found."""
    try:
        from database import SessionLocal
        from models.job import Job

        db = SessionLocal()
        try:
            row = db.get(Job, job_id)
            if row is None:
                return None

            result: dict | None = None
            if row.result_json:
                try:
                    result = json.loads(row.result_json)
                except Exception:
                    pass

            return {
                "id": row.id,
                "status": row.status,
                "logs": [],          # logs are ephemeral; lost on restart
                "result": result,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            db.close()
    except Exception as exc:
        print(f"[JobService] DB read error (job={job_id}): {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API  (identical surface to the old in-memory-only version)
# ─────────────────────────────────────────────────────────────────────────────

def create_job() -> str:
    """Create a new queued job, persist to DB, return its ID."""
    _cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    job = {
        "id": job_id,
        "status": "queued",
        "logs": [],
        "result": None,
        "created_at": now,
    }
    with _lock:
        _jobs[job_id] = job
    _db_write(job_id, "queued")
    return job_id


def get_job(job_id: str) -> dict | None:
    """Return job dict or None.  Checks memory first, then DB."""
    with _lock:
        job = _jobs.get(job_id)
    if job is not None:
        return job
    return _db_read(job_id)


def append_log(job_id: str, message: str) -> None:
    """Append a log line to the in-memory job (not persisted — intentional)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["logs"].append(message)


def set_running(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "running"
    _db_write(job_id, "running")


def set_completed(job_id: str, result: dict) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "completed"
            job["result"] = result
    _db_write(job_id, "completed", result=result)


def set_failed(job_id: str, error: str) -> None:
    err_result = {"error": error}
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "failed"
            job["result"] = err_result
    _db_write(job_id, "failed", result=err_result, error=error)


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery
# ─────────────────────────────────────────────────────────────────────────────

def recover_stale_jobs() -> int:
    """
    Called once in FastAPI lifespan startup.

    Any job still marked "running" or "queued" in the DB from a previous
    process is unreachable — the background thread died with the process.
    Mark them "failed" so the frontend stops polling and shows an error.

    Returns count of recovered (now-failed) jobs.
    """
    try:
        from database import SessionLocal
        from models.job import Job

        db = SessionLocal()
        try:
            stale = db.query(Job).filter(Job.status.in_(["running", "queued"])).all()
            for row in stale:
                row.status = "failed"
                row.error_message = "Server restarted — job lost. Please start a new reel."
                row.updated_at = datetime.utcnow()
            db.commit()
            return len(stale)
        except Exception as exc:
            db.rollback()
            print(f"[JobService] Startup recovery error: {exc}")
            return 0
        finally:
            db.close()
    except Exception as exc:
        print(f"[JobService] Startup recovery session error: {exc}")
        return 0
