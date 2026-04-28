"""
Caption Engine — word-level subtitle synchronisation using faster-whisper.

Why this exists
───────────────
The previous approach computed subtitle timing from word-count proportions,
which drifted 1-3 seconds within a 20-second reel.  When the voice says
"Dubai" at 2.4s but the subtitle shows it at 3.1s, viewers notice immediately.

This module transcribes the generated audio with a local Whisper model to get
exact per-word timestamps, then writes an ASS (Advanced SubStation Alpha)
subtitle file that FFmpeg can burn directly into the video.

ASS over SRT
────────────
ASS supports per-word colour highlighting, custom fonts, drop shadows, and
semi-transparent backgrounds — matching the bold Instagram caption style
without any Python image-compositing overhead.

Installation
────────────
pip install faster-whisper

The "tiny" model (~75 MB) runs on CPU in ~5 s for a 20-second clip and is
accurate enough for word-level timestamps on clean TTS audio.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WordStamp:
    word: str
    start: float
    end: float


@dataclass
class CaptionStyle:
    font_name: str = "Arial Black"
    font_size: int = 47              # 720p (720×1280) — scaled from 1080p value (70 × 720/1080 ≈ 47)
    primary_colour: str = "&H00FFFFFF"   # white  (ASS = &HAABBGGRR)
    outline_colour: str = "&H00000000"   # black
    back_colour: str = "&H00000000"      # unused with BorderStyle=1
    bold: bool = True
    outline_size: int = 3            # 720p outline — scaled from 1080p value (4 × 0.667 ≈ 3)
    shadow_depth: int = 1            # 720p shadow — scaled from 1080p value (2 × 0.667 ≈ 1)
    alignment: int = 5               # centre-screen for high-impact reel captions
    margin_v: int = 100              # 720p lower-third safe zone — scaled from 1080p value (150 × 0.667 ≈ 100)
    margin_lr: int = 40              # 720p left/right safe-zone margin — scaled from 1080p value (60 × 0.667 ≈ 40)


# ─────────────────────────────────────────────────────────────────────────────
# Audio-processing sync constants  (MUST mirror video_engine.py values)
# ─────────────────────────────────────────────────────────────────────────────
#
# Whisper transcribes the RAW MP3 before FFmpeg touches it.
# FFmpeg then applies atempo + adelay to the voice track, which shifts
# WHEN each word is actually heard in the final video.  Without correcting
# the subtitle timestamps for these transformations, captions appear LATE.
#
#   atempo=1.25 → compresses timeline: word at t=1.0s is heard at 0.8s
#   adelay=300ms → pushes voice forward: add 0.30 s to every timestamp
#
# Combined correction: corrected = (original / voice_speed) + voice_delay_s

_AUDIO_SPEED   = 1.25   # must equal _VOICE_SPEED in video_engine.py
_AUDIO_DELAY_S = 0.50   # must equal adelay ms / 1000 in video_engine.py  (500 ms)


# ─────────────────────────────────────────────────────────────────────────────
# Whisper model — loaded once and reused across batch runs
# ─────────────────────────────────────────────────────────────────────────────

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            # "tiny" gives ~95% accuracy on clean TTS audio in ~5 s on CPU
            # Switch to "base" for marginally better accuracy at ~10 s
            _whisper_model = WhisperModel(
                "tiny",
                device="cpu",
                compute_type="int8",   # quantised — 4× faster than float32
                download_root=str(Path(__file__).parent.parent / "storage" / "models"),
            )
        except ImportError:
            pass  # faster-whisper not installed — fall back to estimated timing
    return _whisper_model


# ─────────────────────────────────────────────────────────────────────────────
# Transcription
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_audio(
    audio_path: str,
    script_text: str = "",
    log_handler=None,
) -> list[WordStamp]:
    """
    Return word-level timestamps for the given audio file.

    Parameters
    ----------
    audio_path  : path to the MP3/WAV audio file to transcribe
    script_text : the original LLM-generated script fed as an initial_prompt
                  to faster-whisper.  Acts as a vocabulary cheat-sheet so the
                  model aligns to the exact Hinglish words in the script
                  (e.g. "Spiti", "Uttarakhand", "Dhaba") instead of hallucinating
                  English approximations like "Speedy" or "Outtara Khand".
    log_handler : optional callable for progress messages

    Falls back to evenly-spaced estimation if faster-whisper is unavailable
    or the audio cannot be transcribed.
    """
    def _log(msg: str):
        if log_handler:
            log_handler(msg)
        else:
            print(msg)

    model = _get_whisper_model()
    if model is None:
        _log("faster-whisper not available — using estimated caption timing")
        return []

    # Build the initial_prompt: strip down to plain words so Whisper sees a
    # clean vocabulary list, not ASS markup or formatting symbols.
    initial_prompt = " ".join(script_text.split()) if script_text.strip() else None

    try:
        transcribe_kwargs: dict = {
            "word_timestamps": True,
            "language": "en",
            "beam_size": 1,       # fastest beam for short clips
            "vad_filter": True,   # skip silent gaps
        }
        if initial_prompt:
            # initial_prompt is the faster-whisper equivalent of the OpenAI
            # Whisper API `prompt` parameter.  It primes the decoder with the
            # expected vocabulary so Indian place names / Hinglish words are
            # transcribed correctly rather than being mapped to the nearest
            # English phoneme Whisper has seen in training.
            transcribe_kwargs["initial_prompt"] = initial_prompt

        segments, _info = model.transcribe(audio_path, **transcribe_kwargs)
        words: list[WordStamp] = []
        for seg in segments:
            for w in (seg.words or []):
                cleaned = w.word.strip()
                if cleaned:
                    words.append(WordStamp(word=cleaned, start=w.start, end=w.end))
        _log(f"Transcribed {len(words)} words with timestamps")
        return words
    except Exception as exc:
        _log(f"Whisper transcription failed: {exc} — using estimated timing")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Fallback: estimate timing from script text + audio duration
# ─────────────────────────────────────────────────────────────────────────────

def estimate_word_timestamps(
    script_text: str,
    audio_duration: float,
    hook_duration: float = 2.0,
) -> list[WordStamp]:
    """
    When Whisper is unavailable, distribute words evenly across the voice
    portion of the audio (after the hook screen).
    """
    lines = [l.strip() for l in script_text.splitlines() if l.strip()]
    all_words: list[str] = []
    for line in lines[1:]:  # skip hook line — it plays during the hook screen
        all_words.extend(line.split())

    if not all_words:
        return []

    content_duration = max(audio_duration - hook_duration, 1.0)
    seconds_per_word = content_duration / len(all_words)
    start_offset = hook_duration

    stamps: list[WordStamp] = []
    t = start_offset
    for word in all_words:
        clean = re.sub(r"[^\w$%€£+\-]", "", word)
        if not clean:
            continue
        end = t + seconds_per_word * max(len(clean) / 5, 0.6)
        stamps.append(WordStamp(word=clean, start=t, end=min(end, t + 1.5)))
        t = end

    return stamps


# ─────────────────────────────────────────────────────────────────────────────
# Script-to-timestamp alignment — ensures captions show the LLM's exact words
# ─────────────────────────────────────────────────────────────────────────────

def _tokenize_script(script_text: str) -> list[str]:
    """
    Split the LLM script into a flat word list, preserving Hinglish spelling.
    Strips leading/trailing punctuation from each token so the displayed
    caption doesn't show stray commas or exclamation marks.
    """
    words: list[str] = []
    for line in script_text.splitlines():
        for raw in line.split():
            w = raw.strip()
            # Strip surrounding punctuation but keep internal apostrophes etc.
            w = re.sub(r"^[^\w₹€£$]+|[^\w₹€£$!?]+$", "", w)
            if w:
                words.append(w)
    return words


def _norm_key(w: str) -> str:
    """Lowercase, strip all non-alphanumeric chars for fuzzy matching."""
    return re.sub(r"[^\w]", "", w).lower()


def _align_script_to_timestamps(
    script_words: list[str],
    whisper_words: list[WordStamp],
    audio_duration: float,
) -> list[WordStamp]:
    """
    difflib.SequenceMatcher-based Hard-Sync alignment.

    Problem with naive 1-to-1 array mapping
    ────────────────────────────────────────
    If Whisper transcribes 38 words but the LLM script has 45, a simple
    script[i] → whisper[i] assignment shifts every word after the first
    mismatch by one slot, causing cascading timing drift that compounds until
    the final subtitle is 3-4 seconds late.

    How SequenceMatcher fixes this
    ────────────────────────────────
    SequenceMatcher compares the normalised word sequences and produces
    operation blocks (opcodes) that describe exactly how to transform the
    Whisper sequence into the script sequence:

        equal   → Whisper heard this word correctly.
                  Use its exact start/end timestamp — highest confidence.
        replace → Whisper heard a different word (phonetic mishearing).
                  Distribute the Whisper time-range across the script words
                  proportionally — preserves timing even when Hinglish words
                  like "Spiti" are heard as "Speedy".
        delete  → Whisper completely missed these script words.
                  Timestamps are interpolated between the nearest preceding
                  and following anchored words — no drift because interpolation
                  is always relative to real anchors, not array offsets.
        insert  → Whisper hallucinated extra words not in the script.
                  Ignored — these have no corresponding script word.

    After opcode processing, a gap-fill pass resolves any remaining None
    entries (should be rare), and a minimum-duration clamp ensures even
    extremely fast words stay on screen long enough to be read.
    """
    if not script_words or not whisper_words:
        return list(whisper_words)

    n_s = len(script_words)
    n_w = len(whisper_words)

    # ── Average word duration for fallback interpolation ──
    if n_w >= 2:
        avg_dur = (whisper_words[-1].end - whisper_words[0].start) / n_w
    else:
        avg_dur = max(whisper_words[0].end - whisper_words[0].start, 0.10)
    avg_dur = max(avg_dur, 0.08)

    # ── Normalised keys for SequenceMatcher ──
    s_keys = [_norm_key(w) for w in script_words]
    w_keys = [_norm_key(ws.word) for ws in whisper_words]

    # Sparse map: script index → WordStamp | None (None = needs interpolation)
    aligned_map: list[WordStamp | None] = [None] * n_s

    matcher = difflib.SequenceMatcher(None, s_keys, w_keys, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # Perfect match — use Whisper's exact timestamps one-for-one
            for off in range(i2 - i1):
                si = i1 + off
                wi = j1 + off
                aligned_map[si] = WordStamp(
                    word=script_words[si],
                    start=whisper_words[wi].start,
                    end=whisper_words[wi].end,
                )

        elif tag == "replace":
            # Whisper heard something different — distribute the Whisper
            # time window across however many script words occupy this slot.
            w_start = whisper_words[j1].start
            w_end   = whisper_words[j2 - 1].end
            s_count = i2 - i1
            dur_each = max((w_end - w_start) / s_count, avg_dur)
            for off in range(s_count):
                si = i1 + off
                t0 = w_start + off * dur_each
                aligned_map[si] = WordStamp(
                    word=script_words[si],
                    start=t0,
                    end=t0 + dur_each,
                )

        # "delete" → script words Whisper missed; left as None, interpolated below.
        # "insert" → Whisper hallucination; no script word exists for these, skip.

    # ── Gap-fill: interpolate timestamps for words Whisper missed (delete ops) ──
    # Scan for consecutive None runs and fill with linear interpolation between
    # the real anchors immediately before and after the gap.
    i = 0
    while i < n_s:
        if aligned_map[i] is not None:
            i += 1
            continue

        # Find the extent of this None run
        run_end = i + 1
        while run_end < n_s and aligned_map[run_end] is None:
            run_end += 1

        # Preceding anchor end-time
        if i > 0 and aligned_map[i - 1] is not None:
            prev_t = aligned_map[i - 1].end       # type: ignore[union-attr]
        else:
            prev_t = whisper_words[0].start        # nothing before → start of audio

        # Following anchor start-time
        if run_end < n_s and aligned_map[run_end] is not None:
            next_t = aligned_map[run_end].start    # type: ignore[union-attr]
        else:
            next_t = prev_t + avg_dur * (run_end - i)  # extrapolate forward

        gap_count = run_end - i
        total_gap = max(next_t - prev_t, avg_dur * gap_count)
        word_dur  = total_gap / gap_count

        for k in range(gap_count):
            t0 = prev_t + k * word_dur
            aligned_map[i + k] = WordStamp(
                word=script_words[i + k],
                start=t0,
                end=t0 + word_dur,
            )
        i = run_end

    # ── Safety net: fill any remaining None (shouldn't occur) ──
    for i in range(n_s):
        if aligned_map[i] is None:
            t0 = (audio_duration / n_s) * i
            aligned_map[i] = WordStamp(
                word=script_words[i],
                start=t0,
                end=t0 + avg_dur,
            )

    # ── Minimum display duration: 0.08 s so fast TTS words are readable ──
    MIN_DUR = 0.08
    result: list[WordStamp] = []
    for ws in aligned_map:
        ws = ws  # type: WordStamp
        if ws.end - ws.start < MIN_DUR:
            ws = WordStamp(word=ws.word, start=ws.start, end=ws.start + MIN_DUR)
        result.append(ws)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Timestamp correction — compensate for FFmpeg atempo + adelay
# ─────────────────────────────────────────────────────────────────────────────

def _correct_timestamps(
    words: list[WordStamp],
    voice_speed: float = _AUDIO_SPEED,
    voice_delay_s: float = _AUDIO_DELAY_S,
) -> list[WordStamp]:
    """
    Shift subtitle timestamps to match the audio as it exists in the final video.

    Whisper measures timestamps against the raw, unprocessed MP3.
    FFmpeg applies two transforms that change when each word is actually heard:

      1. atempo=voice_speed compresses the audio timeline uniformly.
         A word at t=1.0 s in the original plays at t=1.0/1.25=0.80 s after
         the 1.25× speed-up.  Divide every timestamp by voice_speed.

      2. adelay pushes the entire voice track forward by voice_delay_s seconds.
         Add voice_delay_s to every timestamp after the speed correction.

    Combined formula:
        corrected = (original / voice_speed) + voice_delay_s

    Without this step, subtitles appear LATE because they reference timestamps
    in the uncompressed audio while the sped-up audio has already moved on.
    """
    result: list[WordStamp] = []
    for ws in words:
        new_start = ws.start / voice_speed + voice_delay_s
        new_end   = ws.end   / voice_speed + voice_delay_s
        result.append(WordStamp(word=ws.word, start=new_start, end=new_end))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ASS time format
# ─────────────────────────────────────────────────────────────────────────────

def _ass_time(seconds: float) -> str:
    """Convert float seconds to ASS timestamp H:MM:SS.cs (centiseconds)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Caption grouping — group words into short phrases for readability
# ─────────────────────────────────────────────────────────────────────────────

def _group_words_into_phrases(
    words: list[WordStamp],
    max_words_per_phrase: int = 4,
    max_gap_seconds: float = 0.6,
) -> list[tuple[float, float, str]]:
    """
    Group word-level stamps into short phrases (start, end, text).

    Rules:
    • Max `max_words_per_phrase` words per subtitle card
    • Split at natural pauses (gap > max_gap_seconds)
    • Result looks like karaoke-style Instagram captions
    """
    if not words:
        return []

    phrases: list[tuple[float, float, str]] = []
    bucket: list[WordStamp] = []

    def _flush():
        if bucket:
            text = " ".join(w.word for w in bucket)
            phrases.append((bucket[0].start, bucket[-1].end, text))
            bucket.clear()

    for i, word in enumerate(words):
        bucket.append(word)
        # Flush if: phrase is full, or there's a notable gap before next word
        next_gap = (words[i + 1].start - word.end) if i + 1 < len(words) else 999
        if len(bucket) >= max_words_per_phrase or next_gap > max_gap_seconds:
            _flush()

    _flush()
    return phrases


# ─────────────────────────────────────────────────────────────────────────────
# ASS subtitle file writer
# ─────────────────────────────────────────────────────────────────────────────

_ASS_HEADER_TEMPLATE = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,2,0,1,{outline_size},{shadow},{align},{ml},{mr},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _highlight_phrase(text: str) -> str:
    """
    Apply yellow ASS colour tag to the longest (most visually important) word
    in a phrase while leaving the remaining words white.

    ASS colour format: &HAABBGGRR (A=alpha, B=blue, G=green, R=red).
    Yellow = B:00 G:FF R:FF → &H0000FFFF   (opaque, no blue, full green+red).
    After the highlighted word, reset to the style's primary white: &H00FFFFFF.

    The `{\c}` reset tag (no value) restores the style's PrimaryColour, so we
    don't hard-code the white value a second time — safer if the style changes.
    """
    words = text.split()
    if not words:
        return text

    # Pick the longest word as the keyword to highlight; if tie, first occurrence
    longest_idx = max(range(len(words)), key=lambda i: len(words[i]))

    result: list[str] = []
    for i, word in enumerate(words):
        if i == longest_idx:
            # Yellow on, word, reset to style primary colour
            result.append(f"{{\\c&H0000FFFF&}}{word}{{\\c}}")
        else:
            result.append(word)
    return " ".join(result)


def write_ass_subtitles(
    words: list[WordStamp],
    output_path: str,
    script_text: str = "",
    audio_duration: float = 20.0,
    hook_duration: float = 2.0,
    style: CaptionStyle | None = None,
    log_handler=None,
) -> str:
    """
    Write an ASS subtitle file from word timestamps (or fall back to phrases
    estimated from the script text).

    Returns the path to the written file.
    """
    def _log(msg: str):
        if log_handler:
            log_handler(msg)
        else:
            print(msg)

    if style is None:
        style = CaptionStyle()

    # Use Whisper timestamps if available, otherwise estimate
    if words:
        # Step 1 — ALIGNMENT: replace Whisper's transcribed words with the
        # original LLM script words, keeping Whisper's per-word timestamps.
        script_words = _tokenize_script(script_text) if script_text.strip() else []
        stamps = _align_script_to_timestamps(script_words, words, audio_duration) if script_words else words

        # ── Stability log: Whisper vs original script word count ─────────────
        _log(f"[SYNC] Script words: {len(script_words)} | "
             f"Whisper words: {len(words)} | "
             f"Delta: {len(script_words) - len(words):+d}")

        # Step 2 — SYNC CORRECTION: Whisper timestamps are from the raw MP3.
        # FFmpeg applies atempo + adelay, shifting when words are actually heard.
        # Divide by voice_speed and add the delay so captions stay on-time.
        stamps = _correct_timestamps(stamps)
    else:
        stamps = estimate_word_timestamps(script_text, audio_duration, hook_duration)

    if not stamps:
        _log("No caption timestamps available — skipping subtitles")
        return ""

    # max 3 words per card — clean Hormozi-style captions, no awkward wrapping
    phrases = _group_words_into_phrases(stamps, max_words_per_phrase=3)
    if not phrases:
        return ""

    bold_flag = "-1" if style.bold else "0"
    header = _ASS_HEADER_TEMPLATE.format(
        font=style.font_name,
        size=style.font_size,
        primary=style.primary_colour,
        secondary="&H00FFFFFF",
        outline=style.outline_colour,
        back=style.back_colour,
        bold=bold_flag,
        outline_size=style.outline_size,
        shadow=style.shadow_depth,
        align=style.alignment,
        ml=style.margin_lr,
        mr=style.margin_lr,
        mv=style.margin_v,
    )

    lines: list[str] = []
    for start, end, text in phrases:
        # Ensure minimum display time of 0.4 s so fast words are readable
        end = max(end, start + 0.40)
        # Upper-case for Instagram reel style, then apply yellow highlight
        caption_text = _highlight_phrase(text.upper())
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{caption_text}"
        )

    out = Path(output_path)
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    _log(f"ASS subtitles written: {len(phrases)} phrases from {len(stamps)} words")
    return str(out)


# ─────────────────────────────────────────────────────────────────────────────
# High-level convenience entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_captions(
    audio_path: str,
    output_ass_path: str,
    script_text: str = "",
    audio_duration: float = 20.0,
    hook_duration: float = 2.0,
    style: CaptionStyle | None = None,
    log_handler=None,
) -> str:
    """
    Transcribe `audio_path`, write an ASS caption file to `output_ass_path`.

    Returns the path on success or "" if captions could not be generated.
    """
    words = transcribe_audio(audio_path, script_text=script_text, log_handler=log_handler)
    return write_ass_subtitles(
        words=words,
        output_path=output_ass_path,
        script_text=script_text,
        audio_duration=audio_duration,
        hook_duration=hook_duration,
        style=style,
        log_handler=log_handler,
    )
