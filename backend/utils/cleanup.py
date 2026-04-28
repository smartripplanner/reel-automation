"""
Storage cleanup utilities — removes orphan files to keep disk usage low.

Called at startup and after each completed/failed job to prevent gradual
accumulation of abandoned clips, temp segments, and old reels.

All functions are safe to call at any time:
  - They never raise — errors are silently swallowed
  - They never delete files that are younger than their respective max_age
  - They return a count of deleted files for logging

Typical file lifecycle
──────────────────────
  storage/videos/scene_*.mp4   : downloaded Pexels clips (deleted by video_engine
                                  during Phase 1; this cleans up any survivors)
  storage/tmp/seg_*.mp4        : FFmpeg intermediate segments (deleted in finally
                                  block of _ffmpeg_render_low_mem; this cleans leftovers)
  storage/reels/reel_*.mp4     : final rendered reels (kept 24 h for download,
                                  then auto-expired to free disk space)
"""

from __future__ import annotations

import time
from pathlib import Path

from utils.storage import BASE_DIR, VIDEOS_DIR, REELS_DIR


def cleanup_old_clips(max_age_hours: float = 1.0) -> int:
    """
    Delete scene clip files older than max_age_hours.

    These are the Pexels downloads in storage/videos/.  The video engine
    already deletes them during Phase 1, but crashes can leave orphans.
    """
    cutoff = time.time() - max_age_hours * 3600
    count = 0
    try:
        for p in VIDEOS_DIR.glob("*.mp4"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    return count


def cleanup_old_reels(max_age_hours: float = 24.0) -> int:
    """
    Delete rendered reel files older than max_age_hours.

    Reels are kept for 24 h so users can download them after generation.
    Older reels are expired to prevent unbounded disk growth on Render's
    ephemeral filesystem.
    """
    cutoff = time.time() - max_age_hours * 3600
    count = 0
    try:
        for p in REELS_DIR.glob("*.mp4"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    return count


def cleanup_temp_segments(max_age_minutes: float = 30.0) -> int:
    """
    Delete orphan FFmpeg segment and concat-list temp files.

    Located in storage/tmp/.  Created and deleted within _ffmpeg_render_low_mem;
    any survivors are from crashed renders.
    """
    tmp_dir = BASE_DIR / "storage" / "tmp"
    if not tmp_dir.exists():
        return 0
    cutoff = time.time() - max_age_minutes * 60
    count = 0
    try:
        for p in tmp_dir.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except Exception:
                pass
    except Exception:
        pass
    return count


def run_full_cleanup(log_handler=None) -> dict:
    """
    Run all cleanup tasks and return a summary dict.

    Safe to call at startup, after job completion, or periodically.
    """
    clips = cleanup_old_clips(1.0)
    reels = cleanup_old_reels(24.0)
    temp  = cleanup_temp_segments(30.0)
    total = clips + reels + temp

    if total > 0:
        msg = f"[Cleanup] Deleted {clips} old clips, {reels} old reels, {temp} temp files"
        print(msg, flush=True)
        if log_handler:
            log_handler(msg)

    return {"clips": clips, "reels": reels, "temp": temp, "total": total}
