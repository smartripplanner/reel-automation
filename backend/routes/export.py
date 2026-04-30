"""
Export Route — returns structured job data for local_renderer.py.

GET /export-job/{job_id}
────────────────────────
Returns everything the local renderer needs to produce a high-quality reel:
  • topic, hook, scenes (with visual_prompt per scene)
  • hashtags, format_type
  • style preset

The local_renderer.py fetches this JSON, downloads clips, generates TTS,
builds ASS subtitles, and runs FFmpeg entirely on the user's machine.
Render never touches heavy video processing.

GET /jobs/{job_id}/download
────────────────────────────
Returns the rendered reel file as an attachment when the reel was generated
locally and the file_path stored in the job result points to a real file on
this server (local dev mode only — Render's ephemeral filesystem means the
file may not exist after a restart).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from services import job_service
from utils.storage import BASE_DIR

router = APIRouter(tags=["export"])


@router.get("/export-job/{job_id}")
def export_job(job_id: str):
    """
    Return the structured script payload for a completed job.

    The job must be in "completed" (or "script_ready") state.
    Pending or failed jobs return 404 / 400 respectively.

    Response shape:
    {
        "job_id": "...",
        "status": "script_ready",
        "topic": "...",
        "hook": "...",
        "scenes": [
            {
                "text": "...",
                "search_query": "italy coast travel",
                "visual_prompt": "cinematic aerial drone italy coast golden hour vertical 9:16",
                "duration": 3.5
            }
        ],
        "hashtags": ["#Travel2026", ...],
        "format_type": "voiceover",
        "style": "cinematic",
        "captions": true
    }
    """
    job = job_service.get_job(job_id)
    if not job:
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

    result = job.get("result") or {}
    scenes_raw = result.get("scenes", [])

    # Normalise scenes — ensure each has text, search_query, visual_prompt, duration
    scenes = []
    for i, s in enumerate(scenes_raw):
        if not isinstance(s, dict):
            continue
        text = s.get("display") or s.get("text") or ""
        sq   = s.get("search_query", "")
        vp   = s.get("visual_prompt", sq)   # already enriched by run_script_pipeline
        scenes.append({
            "text":          text,
            "search_query":  sq,
            "visual_prompt": vp,
            "duration":      round(s.get("duration", 3.5), 2),
        })

    topic    = result.get("topic", job.get("topic", ""))
    hook     = result.get("hook", scenes[0]["text"] if scenes else "")
    hashtags = result.get("hashtags", [])
    fmt      = result.get("format_type", "voiceover")
    script   = result.get("script", "\n".join(s["text"] for s in scenes))

    return {
        "job_id":      job_id,
        "status":      status,
        "topic":       topic,
        "hook":        hook,
        "script":      script,
        "scenes":      scenes,
        "hashtags":    hashtags,
        "format_type": fmt,
        "style":       "cinematic",
        "captions":    True,
        "provider":    result.get("provider", "unknown"),
    }


@router.get("/jobs/{job_id}/download")
def download_reel(job_id: str):
    """
    Download the rendered reel MP4 for a completed job.

    Only works in local dev mode where the reel file is stored on the same
    machine running the server.  On Render the file may not persist across
    restarts (ephemeral filesystem).

    Returns HTTP 404 if the file does not exist on this server.
    """
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

    result = job.get("result") or {}
    file_path = result.get("file_path", "")

    if not file_path:
        raise HTTPException(status_code=404, detail="No reel file path stored for this job")

    # Resolve relative storage paths
    full_path = Path(file_path)
    if not full_path.is_absolute():
        full_path = BASE_DIR / full_path

    if not full_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Reel file not found on server: {file_path}. "
                   "Use local_renderer.py to render the reel on your machine.",
        )

    return FileResponse(
        path=str(full_path),
        media_type="video/mp4",
        filename=full_path.name,
        headers={"Content-Disposition": f'attachment; filename="{full_path.name}"'},
    )
