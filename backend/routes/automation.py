import logging

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


router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/start")
def start_automation_route(db: Session = Depends(get_db)):
    """
    Trigger a single reel via GitHub Actions using the saved niche as topic.

    Reads niche from DB settings, creates one in-memory job record, dispatches
    one workflow_dispatch event to GitHub Actions, and returns immediately.
    Poll GET /automation/jobs to check status.
    """
    from models import Settings as SettingsModel  # local import avoids circular dep

    row = db.query(SettingsModel).first()
    topic = row.niche if row and row.niche else "Travel Tips & Hidden Gems"

    job = create_job(topic)
    logger.info(f"[Start] Dispatching 1 reel to GitHub Actions — topic: {topic!r}, job_id: {job['id']}")

    status_code, response_text = trigger_github_workflow(topic)

    if status_code == 204:
        update_job(job["id"], "running")
        logger.info(f"[Start] Workflow queued (job_id={job['id']})")
    else:
        update_job(job["id"], "failed")
        logger.error(f"[Start] Dispatch failed (HTTP {status_code}): {response_text}")

    return {"status": job["status"], "topic": topic, "job_id": job["id"]}


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
