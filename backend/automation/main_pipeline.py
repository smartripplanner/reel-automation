"""
Main Pipeline — 4-stage orchestrator with format routing and maximum parallelism.

Stage 0 (parallel background) : Trend scrape  ║  Topic + Script AI call
Stage 1 (sequential)          : format_router decides voiceover vs text_music
Stage 2 (parallel)            : Voice/Audio  ║  Media fetch
Stage 3 (sequential)          : Captions (Whisper) → Video render (FFmpeg)

Format routing
──────────────
voiceover  — TTS voice → Whisper captions → narrated reel
text_music — trending IG audio → no TTS → music-driven reel (falls back to
             voiceover if IG audio download fails)

Timing estimates (cold start)
──────────────────────────────
Scrape (Apify, optional): 30-90 s (runs in parallel with Stage 1)
Script AI call:            5-10 s
TTS (edge-tts):            3-5  s  ┐ parallel
Media fetch (Pexels):     10-20 s  ┘
Whisper (local tiny):      3-5  s
FFmpeg render:            30-60 s
Total:                   ~55-90 s   (vs 8-15 min MoviePy in previous version)
"""

from __future__ import annotations

import gc
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from automation.audio_engine import download_trending_audio
from automation.caption_engine import generate_captions
from automation.format_router import build_pipeline_config
from automation.media_engine import fetch_scene_clips, fetch_video_clips
from automation.script_engine import generate_script, _emergency_hinglish_scenes
from automation.scraper_engine import extract_top_hooks, pick_best_audio, scrape_trending_reels
from automation.srt_engine import generate_srt
from automation.topic_engine import generate_topic
from automation.tts_engine import generate_voice, voice_file_exists
from automation.video_engine import create_reel_video
from utils.cleanup import run_full_cleanup
from utils.memory_guard import log_ram, is_memory_critical, is_memory_emergency
from utils.storage import BASE_DIR, ensure_storage_dirs

MEDIA_FETCH_COUNT = 15  # kept for fallback fetch_video_clips calls

# ── Plan-aware memory budget ───────────────────────────────────────────────────
# FREE_PLAN=true  (Render free, 512 MB) : 480p, veryfast, SRT captions, 5 scenes
# FREE_PLAN=false (Render paid / local)  : 720p, ultrafast, ASS captions, 5 scenes
#
# ENABLE_WHISPER: False — faster-whisper loads a 75 MB model into RAM exactly
#   when FFmpeg is also active. The combination always OOMs on free plan.
#   Captions use estimate_word_timestamps() instead — zero model downloads.
#   Re-enable only on plans with 1 GB+ RAM.
_FREE_PLAN     = os.getenv("FREE_PLAN", "false").lower() in ("true", "1", "yes")
MAX_SCENES     = 5
MAX_WORKERS    = 1
ENABLE_WHISPER = False



def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


def _get_audio_duration(voice_path: str) -> float:
    """Return the duration of the voice MP3, or a safe default."""
    try:
        from moviepy.editor import AudioFileClip
        afc = AudioFileClip(str(BASE_DIR / Path(voice_path)))
        dur = afc.duration
        afc.close()
        return dur
    except Exception:
        return 18.0


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
    # generic fallback template words that produce terrible search results
    "nobody", "talks", "part", "tells", "approach", "people", "miss",
    "simple", "action", "information", "decision", "boring", "obvious",
    "consistent", "daily", "habit", "routine", "mistake", "wrong",
}

# Category → curated high-quality Pexels queries.
# These are used when the AI failed and the provider is "fallback" — the
# generic template lines contain no useful proper nouns for image search.
_FALLBACK_MEDIA_QUERIES: dict[str, list[str]] = {
    "salary":    ["professional office career", "business city skyscraper", "laptop work desk"],
    "travel":    ["cinematic travel landscape", "aerial city skyline", "tourist landmark architecture"],
    "money":     ["finance wealth money", "stock market chart city", "luxury lifestyle apartment"],
    "lifestyle": ["aesthetic morning routine", "fitness healthy lifestyle", "minimal modern interior"],
    "general":   ["cinematic nature aerial", "city timelapse night", "abstract aesthetic background"],
}

# Quick keyword → category map for topic-based fallback selection
_FALLBACK_CATEGORY_MAP: dict[str, str] = {
    "salary": "salary", "earn": "salary", "income": "salary", "pay": "salary", "wage": "salary",
    "travel": "travel", "visit": "travel", "trip": "travel", "country": "travel", "move": "travel",
    "money": "money", "save": "money", "invest": "money", "budget": "money", "finance": "money",
    "lifestyle": "lifestyle", "routine": "lifestyle", "habit": "lifestyle", "fitness": "lifestyle",
    "morning": "lifestyle", "productive": "lifestyle",
}


def _fallback_media_query(topic: str) -> str:
    """
    Return a visually rich Pexels query for fallback-script reels.

    Maps the topic to a category, then randomly picks one of the curated
    queries for that category.  Fallback chains prevent any empty result.
    """
    import random as _r
    t = topic.lower()
    category = "general"
    for kw, cat in _FALLBACK_CATEGORY_MAP.items():
        if kw in t:
            category = cat
            break
    candidates = _FALLBACK_MEDIA_QUERIES.get(category, _FALLBACK_MEDIA_QUERIES["general"])
    return _r.choice(candidates)


def _media_query_from_script(
    display_text: str,
    fallback_topic: str,
    provider: str = "ai",
) -> str:
    """
    Derive a Pexels search query from the FINAL locked display script.

    If provider == "fallback" (AI failed, generic template used), skip
    keyword extraction entirely — the template lines contain words like
    "nobody talks about this part" which produce useless image searches.
    Instead return a curated, visually-rich query for the topic category.

    For real AI scripts, extract concrete nouns from the first 3 content
    lines (excluding the CTA line) and deduplicate to ≤5 keywords.
    """
    # Fallback script: use curated category-aware query
    if provider == "fallback":
        return _fallback_media_query(fallback_topic)

    lines = [l.strip() for l in display_text.splitlines() if l.strip()]

    # Drop the last line (CTA: "Save this", "Follow for more", etc.)
    content_lines = lines[:-1] if len(lines) > 1 else lines

    # Tokenise — keep alphanumeric tokens ≥3 chars, skip stop-words
    tokens: list[str] = []
    for line in content_lines[:3]:
        for word in re.findall(r"[a-zA-Z]{3,}", line):
            lower = word.lower()
            if lower not in _MEDIA_STOP_WORDS:
                tokens.append(lower)

    # Deduplicate preserving order, take top 5 keywords
    seen: set[str] = set()
    keywords: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            keywords.append(t)
        if len(keywords) == 5:
            break

    # If keyword extraction still yields garbage (< 2 good words), fall back
    if len(keywords) < 2:
        return _fallback_media_query(fallback_topic)

    return " ".join(keywords)


def _build_hashtags(niche: str) -> list[str]:
    """Convert a niche string to an Apify-friendly hashtag list."""
    base = niche.lower().replace(" ", "").replace("#", "")
    words = niche.lower().split()
    tags = [base] + [w for w in words if len(w) > 3]
    return list(dict.fromkeys(tags))[:5]   # unique, max 5


def run_pipeline(
    topic: str | None = None,
    category_hint: str | None = None,
    hashtags: list[str] | None = None,
    log_handler=None,
) -> dict:
    ensure_storage_dirs()
    run_full_cleanup(log_handler)          # evict orphan clips/segments first
    resolved_topic = topic or category_hint or "general"
    log_ram("Pipeline start", log_handler)

    try:
        # ── Stage 0: Trend scrape (parallel with script generation) ───────────
        trending_reels: list = []
        scrape_hashtags = hashtags or (
            _build_hashtags(category_hint) if category_hint else []
        )

        def _do_scrape():
            if not scrape_hashtags:
                return []
            apify_key = os.getenv("APIFY_API_KEY", "")
            if not apify_key:
                return []
            return scrape_trending_reels(
                hashtags=scrape_hashtags,
                scrape_count=30,
                top_n=5,
                log_handler=log_handler,
            )

        def _do_topic_and_script():
            tp = generate_topic(category_hint or topic, log_handler)
            _log(log_handler, f"Topic: {tp['topic']}")
            sp = generate_script(tp["topic"], log_handler=log_handler)
            return tp, sp

        # Run sequentially (MAX_WORKERS=1 — no parallel threads holding RAM)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            f_scrape = pool.submit(_do_scrape)
            f_script = pool.submit(_do_topic_and_script)
            topic_payload, script_payload = f_script.result()
            trending_reels = f_scrape.result()
        gc.collect()

        resolved_topic = topic_payload["topic"]
        log_ram("After script generation", log_handler)

        # Emergency guard — should never fire; script_engine already handles this
        if not script_payload.get("text", "").strip():
            _log(log_handler, "Script empty — using emergency Hinglish scenes")
            emergency = _emergency_hinglish_scenes(resolved_topic)
            fb_d = [s["display"] for s in emergency]
            fb_v = [s["voice"] for s in emergency]
            script_payload["text"] = "\n".join(fb_d)
            script_payload["voice_text"] = "\n".join(fb_v)
            script_payload["hook"] = fb_d[0]

        display_text = script_payload["text"]
        voice_text = script_payload.get("voice_text", display_text)
        hook_text = script_payload["hook"]

        # Hashtags generated by the LLM (RULE 5) or topic-derived fallback.
        # Stored separately so the raw hook stays clean for DB storage.
        hashtags: list[str] = script_payload.get("hashtags", [])
        if not hashtags:
            from automation.script_engine import _fallback_hashtags
            hashtags = _fallback_hashtags(resolved_topic)

        # ── Enforce scene limit BEFORE anything touches disk or RAM ──────────────
        # The LLM produces 5 scenes; we only fetch/render 3 to stay under 400 MB.
        # Trim the script payloads in-place so every downstream stage sees 3 scenes.
        if script_payload.get("scenes"):
            script_payload["scenes"] = script_payload["scenes"][:MAX_SCENES]
        for key in ("text", "voice_text"):
            if script_payload.get(key):
                lines = script_payload[key].splitlines()
                script_payload[key] = "\n".join(lines[:MAX_SCENES])
        gc.collect()

        # ── Stage 1: Format routing ───────────────────────────────────────────
        pipeline_cfg = build_pipeline_config(
            script_payload=script_payload,
            topic=resolved_topic,
            trending_reels=trending_reels,
            log_handler=log_handler,
        )
        use_tts = pipeline_cfg["use_tts"]
        trending_audio_url = pipeline_cfg.get("trending_audio_url")
        reel_id = resolved_topic.replace(" ", "_")[:40]

        # ── Stage 2: Audio first, then media (sequential to cap RAM) ────────────
        # Previously ran audio+media in parallel (2 workers).
        # With MAX_WORKERS=1, tasks execute one at a time — we trade ~10 s of
        # wall-clock time for ~50 MB of peak RAM savings.
        scenes = script_payload.get("scenes", [])[:MAX_SCENES]
        _log(log_handler, f"Fetching {len(scenes)} scene clips + audio ({pipeline_cfg['format_type']})...")
        voice_path: str = ""
        clip_paths: list[str] = []

        # Audio first — TTS is CPU-bound and light on RAM
        if use_tts:
            voice_path = generate_voice(
                script_text=display_text,
                log_handler=log_handler,
                voice_text=voice_text,
            )
        else:
            dl = download_trending_audio(trending_audio_url, reel_id, log_handler)
            voice_path = dl or generate_voice(
                script_text=display_text,
                log_handler=log_handler,
                voice_text=voice_text,
            )
        gc.collect()

        if not voice_path or not voice_file_exists(voice_path):
            raise RuntimeError("Audio generation produced no audio file.")
        log_ram("After TTS", log_handler)

        # Media fetch second — sequential, one clip at a time (already sequential inside)
        if scenes:
            clip_paths = fetch_scene_clips(scenes, log_handler)
        else:
            clip_paths = fetch_video_clips(resolved_topic, log_handler, count=MAX_SCENES)
        clip_paths = [p for p in clip_paths if (BASE_DIR / Path(p)).exists()]
        gc.collect()
        log_ram("After media fetch", log_handler)

        if not clip_paths:
            _log(log_handler, "No scene clips found — retrying with topic")
            clip_paths = fetch_video_clips(resolved_topic, log_handler, count=MAX_SCENES)
            clip_paths = [p for p in clip_paths if (BASE_DIR / Path(p)).exists()]

        # ── Stage 3a: Captions (estimation-based — no Whisper required) ──────────
        #
        # Plan routing:
        #   FREE_PLAN=true  → SRT subtitles  (lighter, no libass style calc)
        #   FREE_PLAN=false → ASS subtitles  (yellow keyword highlights)
        #
        # Memory guard: if RAM is already critical (>420 MB), skip subtitles
        # entirely — the pipeline still produces a valid video, just without
        # burned-in captions.  This is the last resort before an OOM crash.
        #
        # Both paths use estimate_word_timestamps() — zero model downloads.
        ass_path: str | None = None
        srt_path: str | None = None

        if not use_tts:
            _log(log_handler, "[Captions] Skipped (text_music format — no TTS voice)")
        elif is_memory_critical():
            _log(log_handler, "[Captions] Skipped — RAM critical, conserving memory before FFmpeg")
        else:
            audio_abs      = str(BASE_DIR / Path(voice_path))
            audio_duration = _get_audio_duration(voice_path)
            import tempfile as _tmpmod
            try:
                if _FREE_PLAN:
                    # SRT path — lightweight, no libass style overhead
                    tmp_fd, tmp_srt = _tmpmod.mkstemp(suffix=".srt")
                    os.close(tmp_fd)
                    srt_path = generate_srt(
                        script_text=display_text,
                        audio_duration=audio_duration,
                        output_path=tmp_srt,
                        hook_duration=2.0,
                        log_handler=log_handler,
                    ) or None
                else:
                    # ASS path — yellow highlights, styled captions
                    tmp_fd, tmp_ass = _tmpmod.mkstemp(suffix=".ass")
                    os.close(tmp_fd)
                    ass_path = generate_captions(
                        audio_path=audio_abs,
                        output_ass_path=tmp_ass,
                        script_text=display_text,
                        audio_duration=audio_duration,
                        hook_duration=2.0,
                        log_handler=log_handler,
                    ) or None
                    if ass_path:
                        try:
                            n_lines = sum(1 for _ in open(ass_path, encoding="utf-8"))
                        except Exception:
                            n_lines = 0
                        _log(log_handler, f"[Captions] ASS ready ({n_lines} lines)")
            except Exception as cap_exc:
                _log(log_handler, f"[Captions] Skipped: {cap_exc}")
                ass_path = None
                srt_path = None

        gc.collect()   # free audio buffers before FFmpeg starts
        log_ram("Before FFmpeg", log_handler)

        # Emergency guard: if RAM is still above 460 MB, drop to 360p to survive
        if is_memory_emergency():
            _log(log_handler, "[Memory] EMERGENCY — RAM >460 MB; dropping subtitles + forcing minimal encode")
            ass_path = None
            srt_path = None

        # ── Stage 3b: Video render ────────────────────────────────────────────
        reel_path = create_reel_video(
            clip_paths=clip_paths,
            audio_path=voice_path,
            script_text=display_text,
            ass_path=ass_path,
            srt_path=srt_path,
            log_handler=log_handler,
        )
        gc.collect()
        log_ram("After render", log_handler)
        _log(log_handler, "Reel saved")

        # Clean up temporary subtitle files now that they are burned in
        for sub_file in (ass_path, srt_path):
            if sub_file:
                try:
                    Path(sub_file).unlink(missing_ok=True)
                except Exception:
                    pass

        # ── Delete scene clip files — they are no longer needed ───────────────
        # video_engine._ffmpeg_render_low_mem() already deletes source clips
        # during Phase 1, but some may survive if the render path changed.
        # This is a belt-and-suspenders cleanup.
        for cp in clip_paths:
            try:
                full = BASE_DIR / Path(cp)
                if full.exists():
                    full.unlink()
            except Exception:
                pass
        gc.collect()

        return {
            "status": "completed",
            "file_path": reel_path,
            "caption": hook_text,
            "hashtags": hashtags,
            "topic": resolved_topic,
            "topic_category": topic_payload["category"],
            "script_path": script_payload["script_path"],
            "voice_path": voice_path,
            "media_paths": clip_paths,
            "provider": script_payload.get("provider", "unknown"),
            "format_type": pipeline_cfg["format_type"],
        }

    except Exception as exc:
        _log(log_handler, f"Pipeline failed: {exc}")
        return {
            "status": "failed",
            "file_path": None,
            "caption": f"Generation failed for: {resolved_topic}",
            "topic": resolved_topic,
            "error": str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Script-only pipeline — runs on Render (light, no FFmpeg, no video downloads)
# ─────────────────────────────────────────────────────────────────────────────

def run_script_pipeline(
    topic: str | None = None,
    category_hint: str | None = None,
    log_handler=None,
) -> dict:
    """
    Light pipeline: topic → script → structured JSON.  NO video, NO FFmpeg.

    This is what runs on Render free tier.  The heavy rendering (clip download,
    TTS, FFmpeg) happens in local_renderer.py on the user's local machine.

    Returns a result dict with:
        status        : "script_ready" | "failed"
        topic         : resolved topic string
        scenes        : list of {text, search_query, visual_prompt} dicts
        hashtags      : 5 Instagram hashtags
        format_type   : "voiceover" | "text_music"
        hook          : first scene text (viral hook)
        provider      : "gemini" | "groq" | "emergency"
    """
    ensure_storage_dirs()
    resolved_topic = topic or category_hint or "general"
    log_ram("Script pipeline start", log_handler)

    try:
        # ── Topic generation ──────────────────────────────────────────────────
        topic_payload = generate_topic(category_hint or topic, log_handler)
        resolved_topic = topic_payload["topic"]
        _log(log_handler, f"[Script] Topic: {resolved_topic}")

        # ── Script generation (Gemini → Groq → emergency) ────────────────────
        script_payload = generate_script(resolved_topic, log_handler=log_handler)
        gc.collect()
        log_ram("After script", log_handler)

        scenes = script_payload.get("scenes", [])[:MAX_SCENES]

        # ── Enrich each scene with a cinematic visual prompt for local renderer ─
        for i, scene in enumerate(scenes):
            raw_q = scene.get("search_query", "")
            scene["visual_prompt"] = _build_cinematic_prompt(raw_q, i)

        hashtags = script_payload.get("hashtags", [])
        if not hashtags:
            from automation.script_engine import _fallback_hashtags
            hashtags = _fallback_hashtags(resolved_topic)

        hook = scenes[0]["display"] if scenes else resolved_topic
        _log(log_handler, f"[Script] {len(scenes)} scenes ready — hook: {hook[:60]}")

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
        _log(log_handler, f"Script pipeline failed: {exc}")
        return {
            "status": "failed",
            "topic":  resolved_topic,
            "error":  str(exc),
            "scenes": [],
        }


def _build_cinematic_prompt(raw_query: str, scene_idx: int = 0) -> str:
    """
    Upgrade a bare 2-3 word LLM search query to a rich cinematic Pexels prompt.

    Examples:
        "italy coast" → "cinematic aerial drone italy coast golden hour vertical"
        "office desk" → "cinematic slow motion office desk warm light vertical"

    The enrichment varies across scenes (via scene_idx mod) so consecutive
    queries use different camera styles and don't look repetitive.
    """
    from automation.media_engine import _INDOOR_KEYWORDS  # lazy to avoid circular

    _CAMERA_OUTDOOR = [
        "cinematic aerial drone",
        "drone shot aerial view",
        "cinematic wide angle",
        "slow motion aerial",
    ]
    _CAMERA_INDOOR = [
        "cinematic slow motion",
        "cinematic close up",
        "shallow depth of field",
        "slow motion indoor",
    ]
    _MOOD_OUTDOOR = [
        "golden hour vibrant",
        "blue hour dramatic",
        "natural light cinematic",
        "sunrise dramatic clouds",
    ]
    _MOOD_INDOOR = [
        "warm cinematic light",
        "soft natural light",
        "moody dramatic",
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

    # Build final prompt: camera + subject + mood + format keyword
    return f"{camera} {raw_query} {mood} vertical 9:16 no watermark"
