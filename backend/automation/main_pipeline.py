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
from automation.topic_engine import generate_topic
from automation.tts_engine import generate_voice, voice_file_exists
from automation.video_engine import create_reel_video
from utils.storage import BASE_DIR, ensure_storage_dirs

MEDIA_FETCH_COUNT = 15  # kept for fallback fetch_video_clips calls

# ── Funnel CTA — appended to every Instagram caption ─────────────────────────
# Edit this string to update the CTA across all future reels without touching
# any other pipeline logic.
_IG_CTA = (
    "\n\n✈️ Want the exact day-by-step itinerary for this trip? "
    "Head to the link in my bio to use SmartTripPlannerAI and get your "
    "flights, hotels, and plans sorted in seconds! 🌍"
)


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
    resolved_topic = topic or category_hint or "general"

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

        # Run scrape + topic/script in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_scrape = pool.submit(_do_scrape)
            f_script = pool.submit(_do_topic_and_script)
            topic_payload, script_payload = f_script.result()
            trending_reels = f_scrape.result()

        resolved_topic = topic_payload["topic"]

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

        # ── Stage 2: Voice/Audio + Scene Media in parallel ────────────────────
        scenes = script_payload.get("scenes", [])
        _log(log_handler, f"Fetching {len(scenes)} scene clips + audio ({pipeline_cfg['format_type']})...")
        voice_path: str = ""
        clip_paths: list[str] = []

        def _do_audio():
            if use_tts:
                return generate_voice(
                    script_text=display_text,
                    log_handler=log_handler,
                    voice_text=voice_text,
                )
            # text_music path — download trending IG audio
            dl = download_trending_audio(trending_audio_url, reel_id, log_handler)
            if dl:
                return dl
            _log(log_handler, "IG audio download failed — falling back to TTS")
            return generate_voice(
                script_text=display_text,
                log_handler=log_handler,
                voice_text=voice_text,
            )

        def _do_media():
            # Fetch one video per scene's unique search_query
            if scenes:
                paths = fetch_scene_clips(scenes, log_handler)
            else:
                paths = fetch_video_clips(resolved_topic, log_handler, count=5)
            return [p for p in paths if (BASE_DIR / Path(p)).exists()]

        with ThreadPoolExecutor(max_workers=2) as pool:
            fv = pool.submit(_do_audio)
            fm = pool.submit(_do_media)
            for done in as_completed([fv, fm]):
                if done is fv:
                    voice_path = done.result()
                else:
                    clip_paths = done.result()

        if not voice_path or not voice_file_exists(voice_path):
            raise RuntimeError("Audio generation produced no audio file.")

        if not clip_paths:
            _log(log_handler, "No scene clips found — retrying with topic")
            clip_paths = fetch_video_clips(resolved_topic, log_handler, count=5)
            clip_paths = [p for p in clip_paths if (BASE_DIR / Path(p)).exists()]

        # ── Stage 3: Captions + Render ────────────────────────────────────────
        audio_abs = str(BASE_DIR / Path(voice_path))
        audio_duration = _get_audio_duration(voice_path)

        ass_path: str | None = None
        if pipeline_cfg["use_whisper"]:
            try:
                with tempfile.NamedTemporaryFile(suffix=".ass", delete=False, mode="w") as tmp:
                    tmp_ass = tmp.name
                ass_path = generate_captions(
                    audio_path=audio_abs,
                    output_ass_path=tmp_ass,
                    script_text=display_text,
                    audio_duration=audio_duration,
                    hook_duration=0.0,
                    log_handler=log_handler,
                ) or None
            except Exception as cap_exc:
                _log(log_handler, f"Caption generation skipped: {cap_exc}")
                ass_path = None

        reel_path = create_reel_video(
            clip_paths=clip_paths,
            audio_path=voice_path,
            script_text=display_text,
            ass_path=ass_path,
            log_handler=log_handler,
        )
        _log(log_handler, "Reel saved")

        # ── Cloud Bridge: upload to S3 after successful FFmpeg render ─────────
        video_url: str | None = None
        try:
            from cloud_storage import upload_video_to_cloud   # local import — avoids
            video_url = upload_video_to_cloud(reel_path)      # circular dependency
            if video_url:
                _log(log_handler, f"[CloudStorage] Public URL: {video_url}")
                print(f"[Pipeline] Video uploaded → {video_url}")
            else:
                _log(log_handler, "[CloudStorage] Upload skipped (credentials not configured)")
        except Exception as upload_exc:
            # Upload failure must NEVER fail the pipeline — local file is always kept
            _log(log_handler, f"[CloudStorage] Upload error (non-fatal): {upload_exc}")

        # ── Instagram Publisher: post reel after successful S3 upload ─────────
        # Caption structure (Instagram only):
        #   [hook_text]          ← LLM hook (1 punchy line)
        #   [_IG_CTA]            ← SmartTripPlannerAI funnel CTA
        #   [5 hashtags]         ← LLM-generated or topic-derived (RULE 5)
        # The raw hook_text stays un-modified for DB storage.
        ig_post_id: str | None = None
        _hashtag_block = "\n\n" + " ".join(hashtags) if hashtags else ""
        ig_caption = hook_text + _IG_CTA + _hashtag_block
        if video_url:
            try:
                from services.instagram_poster import upload_reel_to_instagram  # local import
                ig_post_id = upload_reel_to_instagram(
                    video_url=video_url,
                    caption=ig_caption,
                    log_handler=log_handler,
                )
                if ig_post_id:
                    _log(log_handler, f"[Instagram] Reel live — post_id={ig_post_id}")
                else:
                    _log(log_handler, "[Instagram] Publish skipped or failed (non-fatal — reel is saved locally and on S3)")
            except Exception as ig_exc:
                # IG failure must NEVER crash the pipeline
                _log(log_handler, f"[Instagram] Publisher error (non-fatal): {ig_exc}")
        else:
            _log(log_handler, "[Instagram] Skipping publish — no S3 URL available (video not uploaded to cloud)")

        return {
            "status": "completed",
            "file_path": reel_path,
            "video_url": video_url,          # public S3 URL (None if upload skipped)
            "ig_post_id": ig_post_id,        # Instagram Post ID (None if not posted)
            "caption": hook_text,            # raw LLM hook (without CTA) for DB
            "ig_caption": ig_caption,        # full IG caption: hook + CTA + 5 hashtags
            "hashtags": hashtags,            # list[str] — 5 tags generated by LLM
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
