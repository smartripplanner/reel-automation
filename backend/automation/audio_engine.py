"""
Audio Engine — safe Instagram audio download via yt-dlp.

Anti-blocking measures
──────────────────────
1. Rotates realistic mobile User-Agent strings per request
2. Adds randomised sleep between successive downloads (batch safety)
3. Retries with exponential back-off on HTTP 429 / network errors
4. Accepts an optional cookies file for authenticated sessions
5. Falls back gracefully — pipeline continues without trending audio

Installation
────────────
pip install yt-dlp

Cookies (recommended for stable downloads)
──────────────────────────────────────────
Export your Instagram session cookies from Chrome/Firefox using the
"Get cookies.txt LOCALLY" extension, save as backend/storage/ig_cookies.txt,
then set env var: IG_COOKIES_PATH=storage/ig_cookies.txt
"""

from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from utils.storage import AUDIO_DIR, BASE_DIR, ensure_storage_dirs, to_storage_relative

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Mobile User-Agent pool
# ─────────────────────────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Instagram/303.0.0.11.109 Mobile",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/22.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/21E236 Instagram/333.0",
]


def _pick_ua() -> str:
    return random.choice(_USER_AGENTS)


def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# yt-dlp download
# ─────────────────────────────────────────────────────────────────────────────

def _cookies_path() -> str | None:
    """Return absolute path to cookies file if it exists."""
    env_path = os.getenv("IG_COOKIES_PATH")
    if env_path:
        abs_path = BASE_DIR / env_path
        if abs_path.exists():
            return str(abs_path)
    # Default location
    default = BASE_DIR / "storage" / "ig_cookies.txt"
    return str(default) if default.exists() else None


def download_audio(
    url: str,
    output_stem: str,
    max_duration_s: int = 60,
    log_handler=None,
) -> str | None:
    """
    Download the audio track from an Instagram URL using yt-dlp.

    Parameters
    ----------
    url          : Instagram reel / post URL, or direct video URL
    output_stem  : Filename without extension (saved under storage/audio/)
    max_duration_s: Reject audio longer than this (avoids accidentally
                    downloading a 5-minute video)

    Returns
    -------
    Storage-relative path to the MP3 file, or None on failure.
    """
    ensure_storage_dirs()

    try:
        import yt_dlp  # imported here so missing dep doesn't crash other modules
    except ImportError:
        _log(log_handler, "yt-dlp not installed — run: pip install yt-dlp")
        return None

    output_path = AUDIO_DIR / f"{output_stem}.%(ext)s"
    final_path = AUDIO_DIR / f"{output_stem}.mp3"

    cookies = _cookies_path()
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": str(output_path),
        "quiet": True,
        "no_warnings": True,
        "user_agent": _pick_ua(),
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        # Abort if file would be longer than max_duration_s
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration < {max_duration_s}"
        ),
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        # Rate limiting — be polite to Instagram's CDN
        "ratelimit": 1_000_000,   # 1 MB/s max
    }

    if cookies:
        ydl_opts["cookiefile"] = cookies
        _log(log_handler, f"Using cookies: {cookies}")

    for attempt in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if final_path.exists() and final_path.stat().st_size > 0:
                _log(log_handler, f"Audio downloaded: {final_path.name}")
                return to_storage_relative(final_path)

            # yt-dlp may have saved with a different extension — find it
            for f in AUDIO_DIR.glob(f"{output_stem}.*"):
                if f.suffix.lower() in {".mp3", ".m4a", ".aac", ".webm", ".opus"}:
                    _log(log_handler, f"Audio downloaded: {f.name}")
                    return to_storage_relative(f)

        except Exception as exc:
            err_str = str(exc).lower()
            if "rate" in err_str or "429" in err_str:
                wait = 2 ** attempt * random.uniform(1.5, 3.0)
                _log(log_handler, f"Rate limited — waiting {wait:.1f}s before retry {attempt}")
                time.sleep(wait)
            elif "login" in err_str or "authentication" in err_str:
                _log(log_handler, "Instagram requires login — set IG_COOKIES_PATH in .env")
                return None
            else:
                _log(log_handler, f"yt-dlp attempt {attempt} failed: {exc}")
                if attempt < 3:
                    time.sleep(random.uniform(1.0, 2.5))

    _log(log_handler, "Audio download failed after 3 attempts")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Batch helper with polite delays
# ─────────────────────────────────────────────────────────────────────────────

def download_trending_audio(
    audio_url: str | None,
    reel_id: str,
    log_handler=None,
) -> str | None:
    """
    Download trending IG audio for a given reel.
    Adds a small random delay to avoid rate limiting in batch mode.
    """
    if not audio_url:
        _log(log_handler, "No trending audio URL — skipping download")
        return None

    # Polite delay between downloads in batch context
    time.sleep(random.uniform(0.5, 1.5))

    return download_audio(
        url=audio_url,
        output_stem=f"trending_{reel_id}",
        max_duration_s=90,
        log_handler=log_handler,
    )
