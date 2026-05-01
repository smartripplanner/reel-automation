"""
Jobs Route — job status polling + rendered reel serving.

GET /jobs/{job_id}           — poll status, logs, result
GET /jobs/{job_id}/video     — stream the rendered reel MP4 for in-browser playback
GET /jobs/{job_id}/download  — download the rendered reel MP4 as an attachment
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job_status(job_id: str):
    """
    Poll the status of a background reel-generation job.

    Returns
    -------
    {
        "id":     str,
        "status": "queued" | "running" | "completed" | "failed",
        "logs":   [str, ...],
        "result": { file_path, topic, scenes, hashtags, ... } | null
    }
    """
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job


def _resolve_reel_path(job_id: str) -> Path:
    """
    Locate the rendered reel file for a completed job.
    Raises HTTPException if not found or not yet ready.
    """
    job = job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

    status = job.get("status", "")
    if status in ("queued", "running"):
        raise HTTPException(
            status_code=202,
            detail=f"Job {job_id!r} is still {status} — poll again shortly",
        )
    if status == "failed":
        result = job.get("result") or {}
        raise HTTPException(
            status_code=400,
            detail=f"Job {job_id!r} failed: {result.get('error', 'unknown error')}",
        )

    result    = job.get("result") or {}
    file_path = result.get("file_path", "")

    if not file_path:
        raise HTTPException(
            status_code=404,
            detail="No reel file recorded for this job. Rendering may not have completed.",
        )

    # file_path is stored as absolute path by the local pipeline
    full_path = Path(file_path)
    if not full_path.is_absolute():
        # Legacy: was stored relative to backend BASE_DIR
        from utils.storage import BASE_DIR
        full_path = BASE_DIR / full_path

    if not full_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Reel file not found on disk: {file_path}",
        )

    return full_path


@router.get("/{job_id}/video")
def stream_reel(job_id: str):
    """
    Stream the rendered reel MP4 for in-browser <video> playback.

    Returns the file with Content-Type: video/mp4 and no Content-Disposition
    header, so browsers play it inline rather than prompting a download.
    Supports HTTP Range requests (required for seeking in <video> elements).
    """
    full_path = _resolve_reel_path(job_id)
    return FileResponse(
        path=str(full_path),
        media_type="video/mp4",
        filename=full_path.name,
    )


@router.get("/{job_id}/download")
def download_reel(job_id: str):
    """
    Download the rendered reel MP4 as a file attachment.

    The browser will prompt a Save-As dialog instead of playing inline.
    """
    full_path = _resolve_reel_path(job_id)
    return FileResponse(
        path=str(full_path),
        media_type="video/mp4",
        filename=full_path.name,
        headers={"Content-Disposition": f'attachment; filename="{full_path.name}"'},
    )
