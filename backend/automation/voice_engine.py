"""
Voice Engine — neural TTS with edge-tts (primary) and gTTS (fallback).

Why edge-tts over gTTS
──────────────────────
• Microsoft's neural voices (same engine powering Edge browser read-aloud)
• Free, no API key required, no rate limit for personal use
• Dramatically more natural prosody — pauses, emphasis, intonation
• 300+ voices including high-quality en-US-AndrewNeural, AriaNeural, etc.
• Outputs MP3 at 48kHz → no resampling needed for final video

Installation: pip install edge-tts
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from math import ceil
from pathlib import Path

import numpy as np

from utils.storage import AUDIO_DIR, BASE_DIR, ensure_storage_dirs, to_storage_relative

# ─────────────────────────────────────────────────────────────────────────────
# Voice roster — varied so batch reels don't all sound identical
# ─────────────────────────────────────────────────────────────────────────────

_EDGE_VOICES = [
    "en-US-AndrewNeural",           # Male — energetic, great for content
    "en-US-RyanMultilingualNeural", # Male — clear, professional
    "en-US-GuyNeural",              # Male — news-anchor style
    "en-US-AriaNeural",             # Female — natural, conversational
    "en-US-JennyNeural",            # Female — warm and engaging
    "en-US-SaraNeural",             # Female — bright, youthful
]


def _pick_voice() -> str:
    """Pick a voice pseudo-randomly so batch reels have variety."""
    import time
    return _EDGE_VOICES[int(time.time()) % len(_EDGE_VOICES)]


# ─────────────────────────────────────────────────────────────────────────────
# Text preprocessing — strip symbols so TTS reads naturally
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_for_tts(text: str) -> str:
    """
    Convert display-script text or pre-formatted voice text into clean speech.

    Handles both cases:
    • display text with symbols ($, %, €, k, x)
    • voice text that may still have stray formatting
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    processed: list[str] = []

    for line in lines:
        t = line

        # Currency — order matters: do multi-char symbols before single-char
        t = re.sub(r"₹\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " rupees", t)
        t = re.sub(r"€\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " euros", t)
        t = re.sub(r"£\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " pounds", t)
        t = re.sub(r"\$\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " dollars", t)

        # Percentages
        t = re.sub(r"(\d+)\s*%", r"\1 percent", t)

        # Number suffixes
        t = re.sub(r"\b(\d+)k\b", r"\1 thousand", t, flags=re.IGNORECASE)
        t = re.sub(r"\b(\d+)x\b", r"\1 times", t, flags=re.IGNORECASE)

        # Operators and misc symbols
        t = t.replace("+", " plus ")
        t = t.replace("/", " per ")
        t = t.replace("&", " and ")
        t = t.replace("vs", "versus")
        t = t.replace("→", ". ")
        t = t.replace("=", " equals ")
        t = re.sub(r"[#@*•]", " ", t)

        # Collapse whitespace
        t = re.sub(r"\s+", " ", t).strip()

        # Ensure terminal punctuation so TTS pauses between lines
        if t and t[-1] not in ".!?":
            t += "."

        if t:
            processed.append(t)

    return " ".join(processed)


# ─────────────────────────────────────────────────────────────────────────────
# edge-tts async core
# ─────────────────────────────────────────────────────────────────────────────

async def _edge_tts_generate(text: str, output_path: Path, voice: str) -> None:
    """Run edge-tts and save the result as MP3."""
    import edge_tts  # imported here so gTTS-only installs don't crash at import
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def _run_edge_tts(text: str, output_path: Path, voice: str) -> bool:
    """
    Sync wrapper around the async edge-tts call.
    Returns True on success, False on any failure.
    """
    try:
        # Use asyncio.run() when there's no running loop (normal for sync workers).
        # Fall back to get_event_loop() inside async contexts (e.g., uvicorn).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an async context — schedule in the existing loop
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                _edge_tts_generate(text, output_path, voice), loop
            )
            future.result(timeout=30)
        else:
            asyncio.run(_edge_tts_generate(text, output_path, voice))

        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# gTTS fallback
# ─────────────────────────────────────────────────────────────────────────────

def _run_gtts(text: str, output_path: Path) -> bool:
    try:
        from gtts import gTTS
        gTTS(text=text, lang="en", slow=False).save(str(output_path))
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Silent audio fallback
# ─────────────────────────────────────────────────────────────────────────────

def _create_silent_track(output_path: Path, duration_seconds: int) -> None:
    from moviepy.audio.AudioClip import AudioArrayClip
    fps = 44100
    stereo = np.zeros((max(int(duration_seconds * fps), 1), 2), dtype=np.float32)
    clip = AudioArrayClip(stereo, fps=fps)
    try:
        clip.write_audiofile(str(output_path), fps=fps, codec="mp3", logger=None)
    finally:
        clip.close()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_voice(
    script_text: str,
    log_handler=None,
    voice_text: str | None = None,
) -> str:
    """
    Generate an MP3 voice track.

    Priority order
    ──────────────
    1. edge-tts (neural quality, free, no key needed)
    2. gTTS (robotic but reliable, existing dependency)
    3. Silent audio stub (pipeline never crashes)

    Parameters
    ----------
    script_text : str
        Display script — used as fallback text source.
    voice_text : str | None
        Pre-formatted TTS-optimised sentences from script_engine.
        When supplied this is preferred over script_text.
    """
    ensure_storage_dirs()
    fname = f"voice_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp3"
    output_path = AUDIO_DIR / fname

    raw = (voice_text or script_text or "").strip()
    if not raw:
        raw = "Watch this. Start simple. Take one fast action. Follow for more content like this."

    # Always sanitise (handles both display text and pre-formed voice text)
    tts_text = _prepare_for_tts(raw)
    if not tts_text:
        tts_text = "Watch this and follow for more."

    def _log(msg: str) -> None:
        if log_handler:
            log_handler(msg)
        else:
            print(msg)

    # ── Attempt 1: edge-tts (neural) ──
    voice = _pick_voice()
    if _run_edge_tts(tts_text, output_path, voice):
        _log(f"Voice created [edge-tts / {voice}]")
        return to_storage_relative(output_path)

    _log("edge-tts failed → falling back to gTTS")

    # ── Attempt 2: gTTS ──
    if _run_gtts(tts_text, output_path):
        _log("Voice created [gTTS fallback]")
        return to_storage_relative(output_path)

    _log("gTTS failed → using silent audio stub")

    # ── Attempt 3: Silent stub ──
    word_count = len(tts_text.split())
    duration = max(ceil(word_count / 2.8), 8)
    _create_silent_track(output_path, duration)
    _log("Voice created [silent stub]")
    return to_storage_relative(output_path)


def voice_file_exists(relative_path: str) -> bool:
    return (BASE_DIR / relative_path).exists()
