"""
Job Service — SQLite-backed job tracking with in-memory cache layer.

Architecture
────────────
SQLite (WAL mode)  : single source of truth; survives restarts; written by the
                     worker subprocess and read by the FastAPI server.
In-memory dict     : fast read cache for the current server process; avoids a
                     DB round-trip on every poll. Populated on create_job() and
                     updated on each state transition.

Log persistence
───────────────
append_log() now writes to BOTH the in-memory list AND the DB logs_text column.
The DB write uses a raw SQL atomic append (no read-modify-write) so the worker
subprocess can stream logs without any locking conflicts.

After a restart
───────────────
get_job() falls back to the DB when the in-memory dict has no entry.  The DB
row always has the latest status, logs, and result — so GET /jobs/{id} never
returns 404 after a Render restart.

Startup recovery
────────────────
recover_stale_jobs() is called in FastAPI's lifespan startup hook.  Any job
still in "running"/"queued" state from the previous process is marked "failed"
so the frontend stops polling and shows a clear error.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache  (this process only — lost on restart)
# ─────────────────────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_JOB_TTL_HOURS = 24


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
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _db_write(
    job_id: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    topic: str | None = None,
) -> None:
    """Insert-or-update the DB row for job_id.  Never raises."""
    try:
        from database import SessionLocal
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
            if topic is not None:
                row.topic = topic

            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[JobService] DB write error (job={job_id}): {exc}")
        finally:
            db.close()
    except Exception as exc:
        print(f"[JobService] DB session error: {exc}")


def _db_append_log(job_id: str, message: str) -> None:
    """
    Atomically append one log line to jobs.logs_text using SQL string concat.

    SQLite's || operator concatenates strings server-side — no Python
    read-modify-write cycle needed.  Safe for concurrent access from the
    worker subprocess and the FastAPI server.
    """
    try:
        from database import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(
                text("""
                    UPDATE jobs
                    SET    logs_text  = COALESCE(logs_text, '') || :line,
                           updated_at = :now
                    WHERE  id = :job_id
                """),
                {
                    "line":   message + "\n",
                    "now":    datetime.utcnow(),
                    "job_id": job_id,
                },
            )
            conn.commit()
    except Exception:
        pass  # log persistence is best-effort; never crash a worker for this


def _db_read(job_id: str) -> dict | None:
    """Read a job record from DB, including persisted logs."""
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

            # Parse persisted logs — split on newlines, drop empty lines
            logs: list[str] = []
            if row.logs_text:
                logs = [ln for ln in row.logs_text.split("\n") if ln.strip()]

            return {
                "id":         row.id,
                "status":     row.status,
                "topic":      row.topic or "",
                "logs":       logs,
                "result":     result,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            db.close()
    except Exception as exc:
        print(f"[JobService] DB read error (job={job_id}): {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_job(topic: str = "") -> str:
    """Create a new queued job, persist to DB, return its ID."""
    _cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    job = {
        "id":         job_id,
        "status":     "queued",
        "topic":      topic,
        "logs":       [],
        "result":     None,
        "created_at": now,
    }
    with _lock:
        _jobs[job_id] = job
    _db_write(job_id, "queued", topic=topic or None)
    return job_id


def get_job(job_id: str) -> dict | None:
    """
    Return job dict or None.

    Memory cache first (full logs, fast).  Falls back to DB when the job
    was created by a previous process or a worker subprocess.
    """
    with _lock:
        job = _jobs.get(job_id)
    if job is not None:
        return job
    return _db_read(job_id)


def append_log(job_id: str, message: str) -> None:
    """
    Append a log line to in-memory cache AND DB.

    The DB write is best-effort (never raises).  This means logs survive a
    server restart and are visible to the FastAPI server even when the worker
    is a separate subprocess with its own memory space.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["logs"].append(message)
    _db_append_log(job_id, message)


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


def has_running_job() -> bool:
    """True if any job is currently in 'running' state (in-memory or DB)."""
    with _lock:
        for job in _jobs.values():
            if job.get("status") == "running":
                return True
    # Check DB as well (worker subprocess may have set it)
    try:
        from database import SessionLocal
        from models.job import Job
        db = SessionLocal()
        try:
            return db.query(Job).filter(Job.status == "running").first() is not None
        finally:
            db.close()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Startup recovery
# ─────────────────────────────────────────────────────────────────────────────

def recover_stale_jobs() -> int:
    """
    Called once in FastAPI lifespan startup.

    Any job still marked "running" or "queued" in the DB from the previous
    process is now unreachable — mark it "failed" so the frontend stops
    polling and shows a clear error message instead of an infinite spinner.

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
                row.error_message = "Server restarted — job was interrupted. Please start a new reel."
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
