import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from schemas import AutomationStatusResponse, BatchGenerateRequest, BatchGenerateResponse
from services import job_service
from services.automation_service import (
    generate_batch_reels,
    get_status,
    run_generate_job,
    spawn_worker,
    stop_automation,
)
from utils.logger import log_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GenerateRequest(BaseModel):
    topic: str


router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/start")
def start_automation_route(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Generate one reel using the niche saved in Settings.

    Queues the full pipeline (script → TTS → media → captions → FFmpeg →
    S3 → Instagram) as a background task and returns the job_id immediately.
    Poll GET /jobs/{job_id} for live progress logs and final status.
    """
    return run_generate_job(background_tasks, db)


@router.post("/stop", response_model=AutomationStatusResponse)
async def stop_automation_route(db: Session = Depends(get_db)):
    state = await stop_automation(db)
    return AutomationStatusResponse(**state)


@router.post("/generate")
def generate_reel_route(
    req: GenerateRequest,
    db: Session = Depends(get_db),
):
    """
    Generate one reel with an explicit topic.

    Spawns an isolated worker subprocess immediately — the server is never
    blocked by the pipeline and cannot be OOM-killed by FFmpeg rendering.
    Poll GET /jobs/{job_id} for live progress and final status.
    """
    job_id = job_service.create_job(topic=req.topic)
    log_message(db, f"Manual reel queued (subprocess) — topic: {req.topic!r} | job_id={job_id}")
    logger.info(f"[Generate] job_id={job_id} topic={req.topic!r}")
    pid = spawn_worker(job_id, topic=req.topic)
    return {"job_id": job_id, "status": "queued", "topic": req.topic, "worker_pid": pid}


@router.post("/generate-batch", response_model=BatchGenerateResponse)
def generate_batch_reels_route(
    payload: BatchGenerateRequest,
    db: Session = Depends(get_db),
):
    return generate_batch_reels(db, payload.count)


@router.get("/status", response_model=AutomationStatusResponse)
def get_status_route():
    return get_status()
