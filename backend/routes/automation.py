import logging
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from github_worker import trigger_github_workflow
from schemas import (
    AutomationStatusResponse,
    BatchGenerateRequest,
    BatchGenerateResponse,
    GenerateJobResponse,
    GenerateReelResponse,
    SequentialBatchResponse,
)
from services.automation_service import (
    automation_state,
    generate_batch_reels,
    generate_reel,
    get_status,
    run_generate_job,
    stop_automation,
    trigger_sequential_batch,
)
from services.job_tracker import create_job, get_all_jobs, update_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GenerateRequest(BaseModel):
    topic: str


# ── Queued batch dispatcher ────────────────────────────────────────────────────

# Delays between each reel dispatch: job 1 fires immediately,
# job 2 after 15 min, job 3 after another 15 min (30 min total elapsed).
_BATCH_DELAYS = [0, 900, 1800]  # seconds


def _queue_batch_jobs(topic: str) -> None:
    """
    Fire 3 GitHub Actions workflows for the same topic with staggered delays.
    Runs on a daemon thread — never blocks the request/event loop.
    """
    for i, delay in enumerate(_BATCH_DELAYS, start=1):
        logger.info(f"[Queue] Job {i}/3 — waiting {delay}s before dispatch (topic: {topic!r})")
        time.sleep(delay)

        job = create_job(topic)
        logger.info(f"[Queue] Job {i}/3 — dispatching to GitHub Actions (job_id={job['id']})")

        status_code, response_text = trigger_github_workflow(topic)

        if status_code == 204:
            update_job(job["id"], "running")
            logger.info(f"[Queue] Job {i}/3 — workflow queued (job_id={job['id']})")
        else:
            update_job(job["id"], "failed")
            logger.error(
                f"[Queue] Job {i}/3 — dispatch failed (HTTP {status_code}): {response_text}"
            )

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/start")
def start_automation_route(db: Session = Depends(get_db)):
    """
    Non-blocking queued batch trigger.

    Reads the current niche from DB settings and dispatches 3 GitHub Actions
    workflows — immediately, after 15 min, and after 30 min — on a daemon
    thread.  Returns instantly with a confirmation payload so the UI stays
    responsive.  Poll GET /automation/jobs for per-dispatch status.
    """
    from models import Settings as SettingsModel  # local import avoids circular dep

    row = db.query(SettingsModel).first()
    topic = row.niche if row and row.niche else "Travel Tips & Hidden Gems"

    thread = threading.Thread(
        target=_queue_batch_jobs,
        args=(topic,),
        daemon=True,
        name="reel-batch-queue",
    )
    thread.start()

    logger.info(f"[Start] Batch queue started for topic: {topic!r}")
    return {
        "status": "Batch queued",
        "topic": topic,
        "jobs": 3,
        "schedule": "0 min / 15 min / 30 min",
    }


@router.post("/stop", response_model=AutomationStatusResponse)
async def stop_automation_route(db: Session = Depends(get_db)):
    state = await stop_automation(db)
    return AutomationStatusResponse(**state)


@router.post("/generate-async", response_model=SequentialBatchResponse)
async def generate_reel_async_route(db: Session = Depends(get_db)):
    """
    Trigger a sequential batch of 3 reels with 10-minute gaps between each.

    - Reel 1 fires immediately
    - Reel 2 fires in 10 minutes
    - Reel 3 fires in 20 minutes

    Returns immediately with all 3 job_ids.
    Poll GET /jobs/{job_id} for individual status and logs.
    Each reel receives a unique job_id and a random South East Asia sub-topic.
    """
    return trigger_sequential_batch(db)


@router.post("/generate-async/single", response_model=GenerateJobResponse)
async def generate_reel_async_single_route(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Queue a single reel-generation job and return its job_id immediately.
    Poll GET /jobs/{job_id} for status + progress logs.
    Kept for programmatic / testing use — /generate-async now triggers 3 reels.
    """
    return run_generate_job(background_tasks, db)


@router.post("/generate")
def generate_reel_route(req: GenerateRequest):
    """
    Trigger a single reel via GitHub Actions and return a job tracking ID.

    Creates an in-memory job record, dispatches a workflow_dispatch event to
    GitHub Actions (the Muscle), and immediately returns — no blocking wait.
    Poll GET /automation/jobs to see status for all triggered jobs.
    """
    job = create_job(req.topic)
    logger.info(f"[Job {job['id']}] Created for topic: {req.topic!r}")

    status_code, response_text = trigger_github_workflow(req.topic)

    if status_code == 204:
        update_job(job["id"], "running")
        logger.info(f"[Job {job['id']}] GitHub workflow queued successfully")
    else:
        update_job(job["id"], "failed")
        logger.error(
            f"[Job {job['id']}] GitHub workflow trigger failed "
            f"(HTTP {status_code}): {response_text}"
        )

    return {"job_id": job["id"], "status": job["status"]}


@router.get("/jobs")
def list_jobs():
    """Return all in-memory job records (id, topic, status, created_at)."""
    return get_all_jobs()


@router.post("/generate-batch", response_model=BatchGenerateResponse)
async def generate_batch_reels_route(payload: BatchGenerateRequest, db: Session = Depends(get_db)):
    return generate_batch_reels(db, payload.count)


@router.get("/status", response_model=AutomationStatusResponse)
async def get_status_route():
    return get_status()
