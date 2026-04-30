#!/usr/bin/env python3
"""
Pipeline Worker — spawned by the FastAPI server as an isolated subprocess.

Usage (internal — called by automation_service.spawn_worker):
    python worker.py --job-id <uuid> [--category <niche>] [--topic <override>]

Why subprocess isolation
────────────────────────
Running FFmpeg inside the FastAPI server process means an OOM kill by the
Linux kernel (Render free tier: 512 MB hard limit) takes down the entire
server — all active requests die, the health check starts 404ing, and
Render restarts the whole service.  This leaves every in-flight job stuck
in "running" state until the server comes back up and startup recovery runs.

In a subprocess:
  • OOM kills only this worker process (highest RSS = highest OOM score)
  • The FastAPI server stays up, serving health checks and new requests
  • GET /jobs/{id} continues to work — job status is in SQLite, not memory
  • Startup recovery on the next deploy marks interrupted jobs as "failed"
  • The frontend shows a clear error instead of an infinite loading spinner

Log persistence
───────────────
All log messages are written to the DB via job_service.append_log(), which
uses an atomic SQL COALESCE(logs_text,'') || line update.  The FastAPI server
reads the same column on each GET /jobs/{id} poll, so the frontend sees live
progress even though the pipeline runs in a completely separate process.

Render deploy safety
──────────────────
The worker inherits the parent's environment (all .env vars) via
os.environ.copy() in spawn_worker().  It adds the backend directory to
sys.path so all existing automation.* imports work without modification.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── Bootstrap: add backend root to sys.path before any project imports ────────
sys.path.insert(0, str(Path(__file__).parent))

# Suppress noisy warnings before heavy imports
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
os.environ.setdefault("ORT_DISABLE_ALL_LOGGING", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reel pipeline worker subprocess")
    parser.add_argument("--job-id",   required=True, help="Job UUID to update in DB")
    parser.add_argument("--category", default="",    help="Niche/category hint")
    parser.add_argument("--topic",    default="",    help="Topic override (optional)")
    args = parser.parse_args()

    job_id   = args.job_id
    category = args.category.strip() or None
    topic    = args.topic.strip()    or None

    # Initialise DB (creates tables + runs migration if needed)
    from database import init_db
    try:
        init_db()
    except Exception as exc:
        print(f"[Worker] DB init warning: {exc}", flush=True)

    from services import job_service
    from services.settings_service import get_or_create_settings
    from database import SessionLocal
    from automation.main_pipeline import run_script_pipeline
    from utils.memory_guard import log_ram

    # Mark job as running in DB (visible to FastAPI server immediately)
    job_service.set_running(job_id)
    log_ram("Worker start", None)

    def _log(msg: str) -> None:
        """Write a log line to stdout (Render log stream) AND DB (frontend polling)."""
        print(f"[Worker:{job_id[:8]}] {msg}", flush=True)
        job_service.append_log(job_id, msg)

    db = SessionLocal()
    try:
        # Resolve category from settings if not provided
        if not category and not topic:
            settings = get_or_create_settings(db)
            category = settings.niche

        _log(f"Generating script — category={category!r} topic={topic!r}")

        # Script-only pipeline: topic gen + script gen, NO FFmpeg, NO clips.
        # Heavy rendering (clips + TTS + FFmpeg) runs in local_renderer.py
        # on the user's machine so Render never handles RAM-intensive work.
        result = run_script_pipeline(
            topic=topic,
            category_hint=category,
            log_handler=_log,
        )

        status = result.get("status", "script_ready")
        topic_out = result.get("topic", category or topic or "")
        scenes = result.get("scenes", [])

        _log(f"Script ready — {len(scenes)} scenes | topic={topic_out!r}")
        log_ram("Worker end", None)

        # Store the full structured result so /export-job/{id} can serve it
        job_service.set_completed(job_id, {
            "status":      "script_ready",
            "topic":       topic_out,
            "hook":        result.get("hook", ""),
            "scenes":      scenes,
            "hashtags":    result.get("hashtags", []),
            "format_type": result.get("format_type", "voiceover"),
            "provider":    result.get("provider", "unknown"),
            "script_path": result.get("script_path", ""),
            "script":      "\n".join(s.get("display", "") for s in scenes),
        })

    except Exception as exc:
        _log(f"Script generation error: {exc}")
        job_service.set_failed(job_id, str(exc))
        sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()
