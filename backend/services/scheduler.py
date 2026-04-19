"""
Auto-Scheduler — APScheduler BackgroundScheduler
=================================================
Fires the full reel-generation pipeline 3 times a day at fixed UTC times
(09:00, 14:00, 19:00) using the user's saved niche setting.

Architecture
────────────
APScheduler's BackgroundScheduler runs in a daemon thread — it does not
block the FastAPI event loop.  The scheduled job calls _run_pipeline_as_job()
directly (same code path as the /generate-async endpoint) so all logging,
job tracking, and DB writes work identically to a manually triggered run.

Topic selection
───────────────
_run_pipeline_as_job() reads settings.niche from the DB on every run, so
changing the niche in Settings immediately takes effect on the next scheduled
fire — no restart needed.  Fully niche-aware: works for any global niche the
user configures (India, Southeast Asia, Europe, food, finance, etc.).

Usage (wired in main.py lifespan):
    from services.scheduler import start_scheduler, stop_scheduler
    start_scheduler()   # call on startup
    stop_scheduler()    # call on shutdown
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Scheduler singleton
# ─────────────────────────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def _scheduled_reel_job() -> None:
    """
    Executed by APScheduler at 09:00, 14:00, and 19:00 UTC.

    Creates a new job record and runs the full pipeline using the user's
    saved niche setting (settings.niche).  No hardcoded topic list —
    updating the niche in Settings instantly takes effect on the next fire.
    """
    # Import lazily to avoid circular imports at module load time
    from services import job_service                               # noqa: PLC0415
    from services.automation_service import _run_pipeline_as_job  # noqa: PLC0415

    job_id = job_service.create_job()

    logger.info(
        "[Scheduler] Triggered at %s UTC | job_id=%s",
        datetime.utcnow().strftime("%H:%M:%S"),
        job_id,
    )
    print(
        f"[Scheduler] {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC — "
        f"Auto-run started | job_id={job_id}"
    )

    job_service.append_log(job_id, "[Scheduler] Using niche from settings for topic generation")

    # No topic_override — _run_pipeline_as_job reads settings.niche from DB
    _run_pipeline_as_job(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    """Start the BackgroundScheduler and register the 3× daily jobs."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.warning("[Scheduler] Already running — skipping re-start")
        return

    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)

    # 09:00 UTC
    _scheduler.add_job(
        _scheduled_reel_job,
        trigger=CronTrigger(hour=9, minute=0, timezone="UTC"),
        id="reel_09",
        name="Morning auto-reel (09:00 UTC)",
        replace_existing=True,
        misfire_grace_time=300,   # tolerate up to 5 min clock drift
    )
    # 14:00 UTC
    _scheduler.add_job(
        _scheduled_reel_job,
        trigger=CronTrigger(hour=14, minute=0, timezone="UTC"),
        id="reel_14",
        name="Afternoon auto-reel (14:00 UTC)",
        replace_existing=True,
        misfire_grace_time=300,
    )
    # 19:00 UTC
    _scheduler.add_job(
        _scheduled_reel_job,
        trigger=CronTrigger(hour=19, minute=0, timezone="UTC"),
        id="reel_19",
        name="Evening auto-reel (19:00 UTC)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    _scheduler.start()
    logger.info(
        "[Scheduler] Started — jobs scheduled at 09:00, 14:00, 19:00 UTC"
    )
    print("[Scheduler] BackgroundScheduler running — 3× daily auto-reels active (uses settings.niche)")


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler on app shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped")
        print("[Scheduler] BackgroundScheduler stopped")
    _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    """Return the running BackgroundScheduler singleton (None if not started)."""
    return _scheduler
