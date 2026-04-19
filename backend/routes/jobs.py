from fastapi import APIRouter, HTTPException

from services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job_status(job_id: str):
    """
    Poll the status of a background reel-generation job.

    Returns
    -------
    {
        "id": str,
        "status": "queued" | "running" | "completed" | "failed",
        "logs": [str, ...],
        "result": dict | null
    }
    """
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job
