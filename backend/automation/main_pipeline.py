"""
Main Pipeline — local production reel generator.

Stages
──────
1. Topic generation  (topic_engine)
2. Script generation (script_engine → Gemini / Groq / emergency)
3. Voice generation  (tts_engine → ElevenLabs / edge-tts / gTTS)
4. Media fetch       (media_engine → Pexels, unique clips per scene)
5. Caption engine    (caption_engine → ASS with timing estimation)
6. Video render      (video_engine → FFmpeg 720×1280 / CRF22 / medium)

No memory limits. No quality compromises. No Render/cloud architecture.
Everything runs locally with full FFmpeg power.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from automation.caption_engine import generate_captions
from automation.format_router import build_pipeline_config
from automation.media_engine import fetch_scene_clips, fetch_video_clips
from automation.script_engine import generate_script, _emergency_hinglish_scenes
from automation.topic_engine import generate_topic
from automation.tts_engine import generate_voice, voice_file_exists
from automation.video_engine import create_reel_video
from utils.storage import BASE_DIR, ensure_storage_dirs

MAX_SCENES = 7   # support 5-7 scenes

# ─────────────────────────────────────────────────────────────────────────────
# Logging helper
# ─────────────────────────────────────────────────────────────────────────────

def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Audio duration probe
# ─────────────────────────────────────────────────────────────────────────────

def _get_audio_duration(voice_path: str) -> float:
    """Return audio duration in seconds, with a safe default of 20s."""
    try:
        # Try ffprobe first (fast, no heavy import)
        from automation.video_engine import _get_media_duration
        abs_path = str(BASE_DIR / Path(voice_path)) if not Path(voice_path).is_absolute() else voice_path
        dur = _get_media_duration(abs_path)
        if dur > 0:
            return dur
    except Exception:
        pass

    try:
        from moviepy.editor import AudioFileClip
        abs_path = str(BASE_DIR / Path(voice_path)) if not Path(voice_path).is_absolute() else voice_path
        afc = AudioFileClip(abs_path)
        dur = afc.duration
        afc.close()
        return dur
    except Exception:
        return 20.0


# ─────────────────────────────────────────────────────────────────────────────
# Media query helpers
# ─────────────────────────────────────────────────────────────────────────────

_MEDIA_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "this", "that", "these",
    "those", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "must", "shall", "can", "not", "no", "nor", "so", "yet",
    "both", "either", "neither", "each", "every", "all", "any", "few",
    "more", "most", "one", "two", "three", "per", "just", "also", "save",
    "follow", "watch", "know", "here", "there", "than", "then", "when",
    "before", "after", "about", "like", "even", "still", "now", "next",
    "nobody", "talks", "part", "tells", "approach", "people", "miss",
    "simple", "action", "information", "decision", "boring", "obvious",
    "consistent", "daily", "habit", "routine", "mistake", "wrong",
}

_FALLBACK_MEDIA_QUERIES: dict[str, list[str]] = {
    "salary":    ["professional office career", "business city skyscraper", "laptop work desk"],
    "travel":    ["cinematic travel landscape", "aerial city skyline", "tourist landmark architecture"],
    "money":     ["finance wealth money", "stock market chart city", "luxury lifestyle apartment"],
    "lifestyle": ["aesthetic morning routine", "fitness healthy lifestyle", "minimal modern interior"],
    "general":   ["cinematic nature aerial", "city timelapse night", "abstract aesthetic background"],
}

_FALLBACK_CATEGORY_MAP: dict[str, str] = {
    "salary": "salary", "earn": "salary", "income": "salary", "pay": "salary",
    "travel": "travel", "visit": "travel", "trip": "travel", "country": "travel",
    "money": "money", "save": "money", "invest": "money", "finance": "money",
    "lifestyle": "lifestyle", "routine": "lifestyle", "habit": "lifestyle",
}


def _fallback_media_query(topic: str) -> str:
    import random as _r
    t = topic.lower()
    category = "general"
    for kw, cat in _FALLBACK_CATEGORY_MAP.items():
        if kw in t:
            category = cat
            break
    return _r.choice(_FALLBACK_MEDIA_QUERIES.get(category, _FALLBACK_MEDIA_QUERIES["general"]))


def _build_cinematic_prompt(raw_query: str, scene_idx: int = 0) -> str:
    """
    Upgrade a bare LLM search query into a rich cinematic Pexels prompt.

    Examples:
        "italy coast"   → "cinematic aerial drone italy coast golden hour vertical 9:16"
        "office laptop" → "cinematic slow motion office laptop warm light vertical 9:16"
    """
    try:
        from automation.media_engine import _INDOOR_KEYWORDS
    except ImportError:
        _INDOOR_KEYWORDS = {"office", "desk", "kitchen", "gym", "indoor", "home", "room"}

    _CAMERA_OUTDOOR = [
        "cinematic aerial drone",
        "drone shot aerial view",
        "cinematic wide angle landscape",
        "slow motion aerial",
    ]
    _CAMERA_INDOOR = [
        "cinematic slow motion",
        "cinematic close up detail",
        "shallow depth of field",
        "slow motion elegant",
    ]
    _MOOD_OUTDOOR = [
        "golden hour vibrant colors",
        "blue hour dramatic sky",
        "natural light cinematic",
        "sunrise dramatic clouds",
    ]
    _MOOD_INDOOR = [
        "warm cinematic light",
        "soft natural window light",
        "moody dramatic shadows",
        "aesthetic warm tones",
    ]

    q = raw_query.strip().lower()
    is_indoor = any(kw in q for kw in _INDOOR_KEYWORDS)

    if is_indoor:
        camera = _CAMERA_INDOOR[scene_idx % len(_CAMERA_INDOOR)]
        mood   = _MOOD_INDOOR[scene_idx % len(_MOOD_INDOOR)]
    else:
        camera = _CAMERA_OUTDOOR[scene_idx % len(_CAMERA_OUTDOOR)]
        mood   = _MOOD_OUTDOOR[scene_idx % len(_MOOD_OUTDOOR)]

    return f"{camera} {raw_query} {mood} vertical 9:16 no watermark"


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    topic: str | None = None,
    category_hint: str | None = None,
    hashtags: list[str] | None = None,
    log_handler=None,
) -> dict:
    """
    Full local pipeline: script → voice → clips → ASS captions → FFmpeg render.

    Returns
    -------
    dict with keys: status, file_path, topic, hashtags, scenes, provider, format_type
    """
    ensure_storage_dirs()
    resolved_topic = topic or category_hint or "general"

    try:
        # ── Stage 1: Topic generation ────────────────────────────────────────
        _log(log_handler, "[Pipeline] Generating topic...")
        topic_payload = generate_topic(category_hint or topic, log_handler)
        resolved_topic = topic_payload["topic"]
        _log(log_handler, f"[Pipeline] Topic: {resolved_topic}")

        # ── Stage 2: Script generation ────────────────────────────────────────
        _log(log_handler, "[Pipeline] Generating script...")
        script_payload = generate_script(resolved_topic, log_handler=log_handler)

        # Guard: ensure we have scenes
        if not script_payload.get("text", "").strip() and not script_payload.get("scenes"):
            _log(log_handler, "[Pipeline] Script empty — using emergency Hinglish scenes")
            emergency = _emergency_hinglish_scenes(resolved_topic)
            script_payload["scenes"] = emergency
            script_payload["text"] = "\n".join(s["display"] for s in emergency)
            script_payload["voice_text"] = script_payload["text"]
            script_payload["hook"] = emergency[0]["display"]

        # Enforce scene limit
        scenes = script_payload.get("scenes", [])[:MAX_SCENES]
        if not scenes:
            # Build scenes from text lines
            lines = [l.strip() for l in script_payload["text"].splitlines() if l.strip()]
            scenes = [{"display": l, "voice": l, "search_query": resolved_topic} for l in lines[:MAX_SCENES]]

        # Enrich each scene with a cinematic visual prompt
        for i, scene in enumerate(scenes):
            raw_q = scene.get("search_query", resolved_topic)
            scene["visual_prompt"] = _build_cinematic_prompt(raw_q, i)

        display_text  = "\n".join(s.get("display", s.get("text", "")) for s in scenes)
        voice_text    = "\n".join(s.get("voice", s.get("display", "")) for s in scenes)
        hook_text     = scenes[0].get("display", resolved_topic) if scenes else resolved_topic
        result_hashtags = script_payload.get("hashtags", [])
        if not result_hashtags:
            from automation.script_engine import _fallback_hashtags
            result_hashtags = _fallback_hashtags(resolved_topic)

        _log(log_handler, f"[Pipeline] Script ready — {len(scenes)} scenes")

        # ── Stage 3: Format routing ────────────────────────────────────────────
        pipeline_cfg = build_pipeline_config(
            script_payload=script_payload,
            topic=resolved_topic,
            trending_reels=[],
            log_handler=log_handler,
        )
        use_tts = pipeline_cfg.get("use_tts", True)

        # ── Stage 4a: Voice generation ────────────────────────────────────────
        _log(log_handler, "[Pipeline] Generating voice...")
        voice_path = generate_voice(
            script_text=display_text,
            log_handler=log_handler,
            voice_text=voice_text,
        )
        if not voice_path or not voice_file_exists(voice_path):
            raise RuntimeError("Voice generation produced no audio file.")
        _log(log_handler, f"[Pipeline] Voice ready: {voice_path}")

        # ── Stage 4b: Media fetch (unique clips per scene) ────────────────────
        _log(log_handler, f"[Pipeline] Fetching {len(scenes)} clips from Pexels...")
        clip_paths = fetch_scene_clips(scenes, log_handler)
        clip_paths = [
            p for p in clip_paths
            if (Path(p).is_absolute() and Path(p).exists()) or (BASE_DIR / Path(p)).exists()
        ]

        if not clip_paths:
            _log(log_handler, "[Pipeline] Scene clips failed — retrying with topic query")
            clip_paths = fetch_video_clips(resolved_topic, log_handler, count=len(scenes))
            clip_paths = [
                p for p in clip_paths
                if (Path(p).is_absolute() and Path(p).exists()) or (BASE_DIR / Path(p)).exists()
            ]

        if not clip_paths:
            raise RuntimeError("No video clips could be downloaded. Check PEXELS_API_KEY.")
        _log(log_handler, f"[Pipeline] {len(clip_paths)} clips downloaded")

        # ── Stage 5: Caption generation (ASS) ────────────────────────────────
        ass_path: str | None = None
        if use_tts:
            _log(log_handler, "[Pipeline] Generating ASS captions...")
            try:
                voice_abs = (
                    voice_path if Path(voice_path).is_absolute()
                    else str(BASE_DIR / Path(voice_path))
                )
                audio_duration = _get_audio_duration(voice_path)
                tmp_fd, tmp_ass = tempfile.mkstemp(suffix=".ass")
                os.close(tmp_fd)
                ass_path = generate_captions(
                    audio_path=voice_abs,
                    output_ass_path=tmp_ass,
                    script_text=display_text,
                    audio_duration=audio_duration,
                    hook_duration=0.5,    # match adelay=500ms in video_engine
                    log_handler=log_handler,
                ) or None
                if ass_path:
                    n_lines = sum(1 for _ in open(ass_path, encoding="utf-8"))
                    _log(log_handler, f"[Pipeline] ASS subtitles ready ({n_lines} lines)")
            except Exception as cap_exc:
                _log(log_handler, f"[Pipeline] Captions skipped: {cap_exc}")
                ass_path = None
        else:
            _log(log_handler, "[Pipeline] Skipping captions (text_music format)")

        # ── Stage 6: Video render ─────────────────────────────────────────────
        _log(log_handler, "[Pipeline] Rendering reel with FFmpeg...")
        reel_path = create_reel_video(
            clip_paths=clip_paths,
            audio_path=voice_path,
            script_text=display_text,
            ass_path=ass_path,
            log_handler=log_handler,
        )

        # Clean up temporary subtitle file
        if ass_path:
            try:
                Path(ass_path).unlink(missing_ok=True)
            except Exception:
                pass

        _log(log_handler, f"[Pipeline] Reel saved → {reel_path}")

        return {
            "status":      "completed",
            "file_path":   reel_path,
            "topic":       resolved_topic,
            "hook":        hook_text,
            "scenes":      scenes,
            "hashtags":    result_hashtags,
            "format_type": pipeline_cfg.get("format_type", "voiceover"),
            "provider":    script_payload.get("provider", "unknown"),
            "voice_path":  voice_path,
        }

    except Exception as exc:
        _log(log_handler, f"[Pipeline] FAILED: {exc}")
        return {
            "status":    "failed",
            "file_path": None,
            "topic":     resolved_topic,
            "error":     str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Script-only pipeline (kept for compatibility / future light use)
# ─────────────────────────────────────────────────────────────────────────────

def run_script_pipeline(
    topic: str | None = None,
    category_hint: str | None = None,
    log_handler=None,
) -> dict:
    """
    Generate script + scenes only. No video, no TTS, no clips.
    Used for quick topic previewing.
    """
    ensure_storage_dirs()
    resolved_topic = topic or category_hint or "general"

    try:
        topic_payload = generate_topic(category_hint or topic, log_handler)
        resolved_topic = topic_payload["topic"]
        _log(log_handler, f"[Script] Topic: {resolved_topic}")

        script_payload = generate_script(resolved_topic, log_handler=log_handler)
        scenes = script_payload.get("scenes", [])[:MAX_SCENES]

        for i, scene in enumerate(scenes):
            scene["visual_prompt"] = _build_cinematic_prompt(
                scene.get("search_query", ""), i
            )

        hashtags = script_payload.get("hashtags", [])
        if not hashtags:
            from automation.script_engine import _fallback_hashtags
            hashtags = _fallback_hashtags(resolved_topic)

        hook = scenes[0].get("display", resolved_topic) if scenes else resolved_topic

        return {
            "status":      "script_ready",
            "topic":       resolved_topic,
            "hook":        hook,
            "scenes":      scenes,
            "hashtags":    hashtags,
            "format_type": script_payload.get("format_type", "voiceover"),
            "provider":    script_payload.get("provider", "unknown"),
            "script_path": script_payload.get("script_path", ""),
        }

    except Exception as exc:
        _log(log_handler, f"[Script] Pipeline failed: {exc}")
        return {
            "status": "failed",
            "topic":  resolved_topic,
            "error":  str(exc),
            "scenes": [],
        }
