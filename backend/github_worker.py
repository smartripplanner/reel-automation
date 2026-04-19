#!/usr/bin/env python3
"""
GitHub Actions Worker — Standalone Reel Generation Script
==========================================================
Runs the complete reel generation pipeline from the command line.
Designed to execute inside GitHub Actions as the 'Muscle' of the
Hybrid Serverless architecture.

  FastAPI (Render) = Brain  →  schedules, tracks, serves the dashboard
  GitHub Actions   = Muscle →  FFmpeg rendering, S3 upload, IG posting

Usage
-----
    python github_worker.py --topic "Hidden Gems in Europe"
    python github_worker.py --topic "Budget travel in Southeast Asia"

Pipeline stages executed
------------------------
  1. Topic generation  (topic_engine)
  2. LLM script        (Gemini / Groq fallback via script_engine)
  3. ElevenLabs TTS    (tts_engine — eleven_multilingual_v2)
  4. Pexels clip fetch (media_engine)
  5. Whisper captions  (caption_engine — faster-whisper tiny)
  6. FFmpeg render     (video_engine — libx264 + yuv420p + bt709)
  7. AWS S3 upload     (cloud_storage)
  8. Instagram publish (instagram_poster — Graph API v18)

All credentials are read from environment variables.
On GitHub Actions these come from repository Secrets.
Locally they are loaded from backend/.env (if present).

Exit codes
----------
    0 — pipeline completed successfully (reel published)
    1 — pipeline failed (check logs for details)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── Ensure backend/ is on sys.path ────────────────────────────────────────────
# Works whether invoked as:
#   cd backend && python github_worker.py           (SCRIPT_DIR already in path)
#   python backend/github_worker.py                 (need to insert)
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ── Load .env BEFORE any module-level os.getenv() calls ──────────────────────
# safe no-op when .env is absent (e.g., inside GitHub Actions where secrets
# are injected directly as environment variables by the workflow YAML).
from dotenv import load_dotenv  # noqa: E402

load_dotenv(SCRIPT_DIR / ".env", override=False)  # don't overwrite real env vars


# ── Pipeline import (after path + dotenv are set up) ─────────────────────────
from automation.main_pipeline import run_pipeline  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Logging helper — flush every line so GitHub Actions shows progress live
# ─────────────────────────────────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Preflight — fail fast with a clear message if critical secrets are missing
# ─────────────────────────────────────────────────────────────────────────────

_REQUIRED_SECRETS: list[tuple[str, str]] = [
    ("GEMINI_API_KEY",        "LLM script generation"),
    ("PEXELS_API_KEY",        "Scene clip downloads"),
    ("ELEVENLABS_API_KEY",    "Text-to-speech audio"),
    ("AWS_ACCESS_KEY_ID",     "S3 upload"),
    ("AWS_SECRET_ACCESS_KEY", "S3 upload"),
    ("AWS_BUCKET_NAME",       "S3 upload"),
    ("IG_USER_ID",            "Instagram publish"),
    ("IG_ACCESS_TOKEN",       "Instagram publish"),
]


def _check_secrets() -> bool:
    """Return True when all required env vars are set, else print missing list."""
    missing = [
        f"  {name}  ({purpose})"
        for name, purpose in _REQUIRED_SECRETS
        if not os.getenv(name, "").strip()
    ]
    if missing:
        _print("[Worker] ❌ Missing required environment variables:")
        for m in missing:
            _print(m)
        _print("[Worker]    Set them in GitHub Secrets (Actions) or backend/.env (local).")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an Instagram Reel from a topic and post it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python github_worker.py --topic "Hidden Gems in Europe"
  python github_worker.py --topic "Budget travel in Southeast Asia"

Required environment variables (GitHub Secrets or .env):
  GEMINI_API_KEY        — LLM script generation (primary)
  GROQ_API_KEY          — LLM script fallback
  PEXELS_API_KEY        — Scene clip downloads
  ELEVENLABS_API_KEY    — ElevenLabs TTS
  ELEVENLABS_VOICE_ID   — ElevenLabs voice ID
  AWS_ACCESS_KEY_ID     — S3 upload
  AWS_SECRET_ACCESS_KEY — S3 upload
  AWS_BUCKET_NAME       — S3 bucket
  AWS_REGION            — S3 region (e.g. ap-south-1)
  IG_USER_ID            — Instagram numeric user ID
  IG_ACCESS_TOKEN       — Instagram Graph API long-lived token
        """,
    )
    parser.add_argument(
        "--topic",
        required=True,
        help='Topic string for the reel (e.g., "Hidden Gems in Europe")',
    )
    args = parser.parse_args()

    topic: str = args.topic.strip()
    if not topic:
        _print("[Worker] ERROR: --topic cannot be empty.")
        return 1

    _print("=" * 60)
    _print("[Worker] Reel Automation — GitHub Actions Worker")
    _print("=" * 60)
    _print(f"[Worker] Topic : {topic}")
    _print(f"[Worker] Runner: GitHub Actions (ubuntu-latest)")
    _print("")

    # Fail fast on missing secrets — avoids wasted pipeline time
    if not _check_secrets():
        return 1

    # ── Run the full pipeline ─────────────────────────────────────────────────
    _print("[Worker] Starting pipeline...")

    def log_handler(msg: str) -> None:
        # All pipeline log lines are prefixed and flushed so GH Actions
        # shows them in real time rather than buffering until the end.
        _print(f"[Pipeline] {msg}")

    result = run_pipeline(
        topic=topic,
        category_hint=topic,   # use the exact topic — no template re-mapping
        log_handler=log_handler,
    )

    # ── Evaluate result ───────────────────────────────────────────────────────
    status     = result.get("status", "unknown")
    file_path  = result.get("file_path") or "N/A"
    video_url  = result.get("video_url") or "Not uploaded to S3"
    ig_post_id = result.get("ig_post_id") or "Not posted to Instagram"
    topic_used = result.get("topic", topic)

    _print("")
    _print("=" * 60)

    if status == "failed":
        error = result.get("error", "Unknown pipeline error")
        _print(f"[Worker] ❌ PIPELINE FAILED")
        _print(f"[Worker] Error      : {error}")
        _print(f"[Worker] Topic      : {topic_used}")
        _print("=" * 60)
        return 1

    _print(f"[Worker] ✅ PIPELINE COMPLETE")
    _print(f"[Worker] Topic      : {topic_used}")
    _print(f"[Worker] Local file : {file_path}")
    _print(f"[Worker] S3 URL     : {video_url}")
    _print(f"[Worker] Instagram  : {ig_post_id}")
    _print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
