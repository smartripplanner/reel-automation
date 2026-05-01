#!/usr/bin/env python3
"""
Pipeline Worker — spawned by the FastAPI server as an isolated subprocess.

Usage (internal — called by automation_service.spawn_worker):
    python worker.py --job-id <uuid> [--category <niche>] [--topic <override>]

Why subprocess isolation
────────────────────────
Running the full pipeline (FFmpeg, TTS, clip downloads) inside the FastAPI
server process would block all other API requests during rendering.  Using a
subprocess means:
  • The FastAPI server stays fully responsive while rendering
  • Progress logs are written to SQLite and polled by the frontend
  • If the worker crashes, the server keeps running unaffected
  • Job status is always recoverable from the database

Log persistence
───────────────
All log messages go to stdout AND to the DB via job_service.append_log().
The FastAPI server reads the same logs_text column on each GET /jobs/{id} poll,
so the frontend sees live progress even though rendering is in a separate process.
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
    from automation.main_pipeline import run_pipeline

    # Mark job as running in DB (visible to FastAPI server immediately)
    job_service.set_running(job_id)

    def _log(msg: str) -> None:
        """Write a log line to stdout AND to DB (frontend polling)."""
        print(f"[Worker:{job_id[:8]}] {msg}", flush=True)
        job_service.append_log(job_id, msg)

    db = SessionLocal()
    try:
        # Resolve category from settings if neither topic nor category supplied
        if not category and not topic:
            settings = get_or_create_settings(db)
            category = settings.niche

        _log(f"Starting full pipeline — topic={topic!r} category={category!r}")

        # Run the complete local pipeline:
        # script → voice → clips → ASS captions → FFmpeg render → output/reel_*.mp4
        result = run_pipeline(
            topic=topic,
            category_hint=category,
            log_handler=_log,
        )

        status    = result.get("status", "completed")
        file_path = result.get("file_path")
        topic_out = result.get("topic", category or topic or "")
        scenes    = result.get("scenes", [])
        error     = result.get("error")

        if status == "failed" or error:
            _log(f"Pipeline failed: {error}")
            job_service.set_failed(job_id, error or "Unknown error")
            sys.exit(1)

        _log(f"Pipeline complete — {len(scenes)} scenes | topic={topic_out!r}")
        if file_path:
            _log(f"Reel saved → {file_path}")

        # Store full result so GET /jobs/{id} and GET /jobs/{id}/download work
        job_service.set_completed(job_id, {
            "status":      "completed",
            "file_path":   file_path,
            "topic":       topic_out,
            "hook":        result.get("hook", ""),
            "scenes":      scenes,
            "hashtags":    result.get("hashtags", []),
            "format_type": result.get("format_type", "voiceover"),
            "provider":    result.get("provider", "unknown"),
            "voice_path":  result.get("voice_path", ""),
        })

    except Exception as exc:
        _log(f"Worker error: {exc}")
        job_service.set_failed(job_id, str(exc))
        sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()
