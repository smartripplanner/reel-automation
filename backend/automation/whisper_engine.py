"""
Whisper Engine — word-level timestamp extraction.

Two backends, tried in priority order
──────────────────────────────────────
1. OpenAI Whisper API  (cloud, accurate, costs ~$0.0001 / 20s clip)
   Requires: OPENAI_API_KEY
   Uses:     timestamp_granularities=["word"] in verbose_json mode

2. faster-whisper      (local CPU, free, ~5s for 20s clip with tiny model)
   Requires: pip install faster-whisper  (already installed)
   Model:    tiny (75MB, auto-downloaded on first run)

Both return the same normalised list[WordStamp] so downstream code
(caption_renderer) is backend-agnostic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# Shared data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WordStamp:
    word: str
    start: float   # seconds from audio start
    end: float     # seconds from audio start


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Backend 1 — OpenAI Whisper API
# ─────────────────────────────────────────────────────────────────────────────

def _transcribe_openai(audio_path: str, log_handler=None) -> list[WordStamp]:
    """
    Call OpenAI Whisper API with timestamp_granularities=["word"].

    Returns word-level stamps. Falls back to [] on any error.

    API docs:
      https://platform.openai.com/docs/guides/speech-to-text/timestamps
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        with open(audio_path, "rb") as f:
            # verbose_json + word granularity → response.words list
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )

        # response.words is list of TranscriptionWord objects (word, start, end)
        words: list[WordStamp] = []
        for w in (response.words or []):
            cleaned = w.word.strip()
            if cleaned:
                words.append(WordStamp(word=cleaned, start=w.start, end=w.end))

        _log(log_handler, f"Whisper [OpenAI API]: {len(words)} words transcribed")
        return words

    except ImportError:
        _log(log_handler, "openai package not installed — trying local Whisper")
        return []
    except Exception as exc:
        _log(log_handler, f"OpenAI Whisper failed: {exc} — trying local fallback")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Backend 2 — faster-whisper (local)
# ─────────────────────────────────────────────────────────────────────────────

_local_model = None   # module-level singleton — loaded once, reused across calls


def _get_local_model():
    global _local_model
    if _local_model is None:
        try:
            from faster_whisper import WhisperModel
            model_dir = str(Path(__file__).parent.parent / "storage" / "models")
            # "tiny" = 75 MB, ~5s on CPU for a 20s clip
            # Switch to "base" (~150 MB) for marginally better accuracy
            _local_model = WhisperModel(
                "tiny",
                device="cpu",
                compute_type="int8",
                download_root=model_dir,
            )
        except ImportError:
            pass
    return _local_model


def _transcribe_local(audio_path: str, log_handler=None) -> list[WordStamp]:
    """
    Transcribe locally with faster-whisper (tiny INT8 model).
    """
    model = _get_local_model()
    if model is None:
        _log(log_handler, "faster-whisper not available — run: pip install faster-whisper")
        return []

    try:
        segments, _info = model.transcribe(
            audio_path,
            word_timestamps=True,
            language="en",
            beam_size=1,       # fastest inference for short clips
            vad_filter=True,   # skip silence
        )
        words: list[WordStamp] = []
        for seg in segments:
            for w in (seg.words or []):
                cleaned = w.word.strip()
                if cleaned:
                    words.append(WordStamp(word=cleaned, start=w.start, end=w.end))

        _log(log_handler, f"Whisper [local tiny]: {len(words)} words transcribed")
        return words

    except Exception as exc:
        _log(log_handler, f"Local Whisper failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Estimation fallback (no Whisper at all)
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_timestamps(
    script_text: str,
    audio_duration: float,
    hook_duration: float = 2.0,
) -> list[WordStamp]:
    """
    Distribute words evenly across the voice portion of the audio.
    Used when both Whisper backends are unavailable.
    """
    lines = [l.strip() for l in script_text.splitlines() if l.strip()]
    all_words = []
    for line in lines[1:]:   # skip hook (plays during hook screen)
        all_words.extend(line.split())

    if not all_words:
        return []

    content_dur = max(audio_duration - hook_duration, 1.0)
    secs_per_word = content_dur / len(all_words)
    t = hook_duration
    stamps: list[WordStamp] = []
    for word in all_words:
        import re
        clean = re.sub(r"[^\w$%€£+\-]", "", word)
        if not clean:
            continue
        end = t + max(secs_per_word * max(len(clean) / 5, 0.6), 0.15)
        stamps.append(WordStamp(word=clean, start=t, end=min(end, t + 1.5)))
        t = end
    return stamps


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_word_timestamps(
    audio_path: str,
    script_text: str = "",
    audio_duration: float = 20.0,
    hook_duration: float = 2.0,
    log_handler=None,
) -> list[WordStamp]:
    """
    Return word-level timestamps for the given audio file.

    Priority: OpenAI API → local faster-whisper → estimation
    """
    # Try OpenAI API first
    stamps = _transcribe_openai(audio_path, log_handler)
    if stamps:
        return stamps

    # Try local faster-whisper
    stamps = _transcribe_local(audio_path, log_handler)
    if stamps:
        return stamps

    # Fall back to estimation
    _log(log_handler, "Using estimated word timing (no Whisper available)")
    return _estimate_timestamps(script_text, audio_duration, hook_duration)
