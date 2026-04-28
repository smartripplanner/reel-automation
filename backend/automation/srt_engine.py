"""
SRT Engine — lightweight subtitle generation for Render free plan.

Why SRT instead of ASS on free plan
──────────────────────────────────
ASS (Advanced SubStation Alpha) subtitles require FFmpeg to link against
libass, which adds ~10-15 MB of parser/renderer RAM overhead per encode.
On Render free (512 MB hard limit), every megabyte counts.

SRT (SubRip Text) is plain UTF-8 — sequence number, timestamps, text.
FFmpeg's `subtitles=` filter renders SRT without the libass overhead for
styling (no colour tags, no custom fonts, just clean white text with a
configurable outline rendered by the chosen codec).

Quality comparison
──────────────────
  SRT  : white text, system font, no colour per-word.  Clean and readable.
  ASS  : yellow keyword highlight, Arial Black, drop shadow.  More polished.

On a Render free plan the user sees SRT captions (clean).
On paid / 1 GB+, the pipeline uses ASS with yellow highlights.

No Whisper required
───────────────────
Like the ASS path, this module uses estimate_word_timestamps() which
distributes script words proportionally across audio duration — zero model
downloads, ~0 RAM overhead.
"""

from __future__ import annotations

from pathlib import Path

from automation.caption_engine import (
    WordStamp,
    _group_words_into_phrases,
    estimate_word_timestamps,
)


def _srt_time(seconds: float) -> str:
    """Convert float seconds to SRT timestamp: HH:MM:SS,mmm"""
    ms  = int((seconds % 1) * 1000)
    s   = int(seconds % 60)
    m   = int((seconds // 60) % 60)
    h   = int(seconds // 3600)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(
    script_text: str,
    audio_duration: float,
    output_path: str,
    hook_duration: float = 2.0,
    log_handler=None,
) -> str:
    """
    Generate an SRT subtitle file from script text + audio duration.

    Parameters
    ----------
    script_text    : Full script text (used for word distribution)
    audio_duration : Total audio duration in seconds
    output_path    : Where to write the .srt file
    hook_duration  : Seconds to skip at the start (hook screen has no captions)
    log_handler    : Optional callable for progress messages

    Returns the output path on success, "" on failure.
    """
    def _log(msg: str):
        if log_handler:
            log_handler(msg)
        else:
            print(msg)

    stamps = estimate_word_timestamps(script_text, audio_duration, hook_duration)
    if not stamps:
        _log("[SRT] No timestamps — skipping subtitles")
        return ""

    phrases = _group_words_into_phrases(stamps, max_words_per_phrase=3)
    if not phrases:
        return ""

    blocks: list[str] = []
    for i, (start, end, text) in enumerate(phrases, start=1):
        end = max(end, start + 0.40)
        blocks.append(f"{i}")
        blocks.append(f"{_srt_time(start)} --> {_srt_time(end)}")
        blocks.append(text.upper())
        blocks.append("")   # mandatory blank line between entries

    out = Path(output_path)
    out.write_text("\n".join(blocks), encoding="utf-8")
    _log(f"[SRT] {len(phrases)} subtitle phrases → {output_path}")
    return str(out)
