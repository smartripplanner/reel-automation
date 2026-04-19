from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from database import get_db
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

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/start", response_model=SequentialBatchResponse)
def start_automation_route(db: Session = Depends(get_db)):
    """
    Non-blocking batch trigger.

    Queues 3 reels sequentially (each starts after the previous completes)
    and returns immediately with 3 job IDs — the pipeline never runs on the
    request thread.  Poll GET /jobs/{job_id} for per-reel progress.

    Previous behaviour: called start_automation() which launched
    _automation_loop() via asyncio.create_task().  Because _automation_loop
    called the synchronous _generate_reel_record() inside an async coroutine
    without run_in_executor, it blocked the entire FastAPI event loop for the
    full pipeline duration (~90 s), making the server unresponsive.
    """
    return trigger_sequential_batch(db)


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


@router.post("/generate", response_model=GenerateReelResponse)
async def generate_reel_route(db: Session = Depends(get_db)):
    """Synchronous generate — kept for backwards compatibility."""
    return generate_reel(db)


@router.post("/generate-batch", response_model=BatchGenerateResponse)
async def generate_batch_reels_route(payload: BatchGenerateRequest, db: Session = Depends(get_db)):
    return generate_batch_reels(db, payload.count)


@router.get("/status", response_model=AutomationStatusResponse)
async def get_status_route():
    return get_status()
