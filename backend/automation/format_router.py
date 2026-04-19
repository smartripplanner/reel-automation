"""
Format Router — decides voiceover vs text_music pipeline path.

voiceover  (default)
    • AI generates both display[] and voice[] script lines
    • TTS engine synthesises voice audio (OpenAI → ElevenLabs → edge-tts → gTTS)
    • Whisper extracts word-level timestamps for subtitle sync
    • FFmpeg renders video with burned-in ASS captions

text_music
    • AI generates display[] lines only (lyrics-style text overlays)
    • Trending Instagram audio downloaded via yt-dlp (no TTS)
    • Captions are static line-by-line text cards, no Whisper needed
    • FFmpeg renders video with text overlays synced to music

The format_type field returned by script_engine drives the switch.
Falls back to "voiceover" if format_type is absent, unknown, or if
IG audio download fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.scraper_engine import TrendingReel


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FORMAT_VOICEOVER = "voiceover"
FORMAT_TEXT_MUSIC = "text_music"

# Topics / categories that typically suit trending music over narration
_MUSIC_KEYWORDS = {
    "dance", "vibe", "trend", "aesthetic", "glow", "transformation",
    "reveal", "outfit", "fashion", "style", "gym", "workout", "fitness",
    "morning", "routine", "recipe", "cook", "food", "asmr", "satisfying",
}


def _infer_format_from_topic(topic: str) -> str:
    """
    Heuristic: if the topic touches lifestyle/visual content, prefer text_music.
    Everything financial/educational → voiceover.
    """
    words = set(topic.lower().split())
    if words & _MUSIC_KEYWORDS:
        return FORMAT_TEXT_MUSIC
    return FORMAT_VOICEOVER


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def resolve_format(
    script_payload: dict,
    topic: str = "",
) -> str:
    """
    Return the resolved format_type string for this reel.

    Priority:
    1. Explicit format_type from script_engine AI output
    2. Heuristic inference from topic keywords
    3. Default: voiceover
    """
    fmt = (script_payload.get("format_type") or "").strip().lower()
    if fmt in {FORMAT_VOICEOVER, FORMAT_TEXT_MUSIC}:
        return fmt
    if topic:
        return _infer_format_from_topic(topic)
    return FORMAT_VOICEOVER


def pick_trending_audio(reels: list) -> str | None:
    """
    Return the best audio URL from a list of TrendingReel objects.
    Picks the highest-view reel that has a usable audio URL.
    """
    for reel in reels:
        if getattr(reel, "audio_url", None):
            return reel.audio_url
    return None


def build_pipeline_config(
    script_payload: dict,
    topic: str,
    trending_reels: list,
    log_handler=None,
) -> dict:
    """
    Assemble a pipeline configuration dict consumed by main_pipeline.

    Returns
    -------
    {
        "format_type": "voiceover" | "text_music",
        "trending_audio_url": str | None,   # only for text_music
        "use_tts": bool,                    # True for voiceover
        "use_whisper": bool,                # True for voiceover
    }
    """
    def _log(msg):
        if log_handler:
            log_handler(msg)
        else:
            print(msg)

    fmt = resolve_format(script_payload, topic)
    audio_url: str | None = None

    if fmt == FORMAT_TEXT_MUSIC:
        audio_url = pick_trending_audio(trending_reels)
        if not audio_url:
            _log("text_music selected but no trending audio available — falling back to voiceover")
            fmt = FORMAT_VOICEOVER

    _log(f"Pipeline format: {fmt}")

    return {
        "format_type": fmt,
        "trending_audio_url": audio_url,
        "use_tts": fmt == FORMAT_VOICEOVER,
        "use_whisper": fmt == FORMAT_VOICEOVER,
    }
