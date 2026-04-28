import os
import re
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils.pillow_compat import ensure_pillow_compat
from moviepy.editor import ColorClip

from utils.storage import VIDEOS_DIR, ensure_storage_dirs, to_storage_relative


load_dotenv()
ensure_pillow_compat()
MEDIA_CACHE: dict[tuple[str, int], list[str]] = {}

# Stop words that pollute Pexels search and return irrelevant clips
_STOP_WORDS = {
    "best", "top", "most", "why", "how", "what", "for", "and", "the",
    "in", "of", "to", "a", "an", "vs", "or", "is", "are", "on", "at",
    "with", "by", "that", "this", "from", "where", "when", "people",
    "more", "less", "your", "you", "our", "my", "their", "its",
    "which", "who", "been", "has", "have", "was", "were", "will",
    "about", "before", "after", "than", "then", "if", "but", "so",
    "higher", "lower", "bigger", "smaller", "faster", "better", "worse",
    "country", "countries", "city", "cities", "world", "global", "local",
    "year", "month", "week", "day", "time", "right", "left", "right",
    "actually", "really", "just", "only", "even", "also", "still",
}

# Topic keyword → better Pexels search terms
# Keys are matched against the full topic string (longest match wins).
# Values are the actual Pexels query that will be sent to the API.
_KEYWORD_MAP = {
    # ── Lifestyle / content niches ──────────────────────────────────────────
    "digital nomad": "laptop coffee work",
    "remote work": "laptop work home office",
    "side hustle": "entrepreneur business laptop",
    "passive income": "money online business",
    "real estate": "real estate property house",
    "morning routine": "morning sunrise bedroom",
    "productivity": "productivity desk focus",
    "fitness": "fitness workout gym",
    "morning": "morning routine sunrise",
    "lifestyle": "lifestyle modern city",
    "nomad": "remote work laptop",
    "travel": "travel adventure",
    "career": "career business professional",
    "coding": "programming code developer",
    "design": "designer creative studio",
    "crypto": "technology digital finance",
    "invest": "stock market investing",
    "budget": "budget saving money",
    "wealth": "luxury wealth success",
    "salary": "office work business",
    "money": "money cash finance",
    # ── Countries / regions ─────────────────────────────────────────────────
    "south korea": "south korea seoul cityscape",
    "north korea": "north korea architecture",
    "new zealand": "new zealand nature landscape",
    "czech republic": "prague europe city",
    "saudi arabia": "riyadh saudi arabia skyline",
    "south africa": "cape town south africa",
    "united kingdom": "london uk city",
    "united states": "new york city usa",
    "vietnam": "vietnam hanoi city street",
    "thailand": "thailand bangkok temple",
    "indonesia": "indonesia bali beach",
    "malaysia": "malaysia kuala lumpur skyline",
    "philippines": "philippines beach island",
    "mexico": "mexico city colourful street",
    "colombia": "colombia medellin city",
    "argentina": "argentina buenos aires city",
    "brazil": "brazil rio de janeiro",
    "peru": "peru machu picchu nature",
    "morocco": "morocco marrakech market",
    "egypt": "egypt cairo pyramids",
    "kenya": "kenya africa safari",
    "nigeria": "nigeria lagos city",
    "turkey": "turkey istanbul city",
    "greece": "greece santorini island",
    "italy": "italy rome architecture",
    "spain": "spain barcelona city",
    "france": "france paris eiffel",
    "netherlands": "netherlands amsterdam canal",
    "poland": "poland warsaw city",
    "ukraine": "ukraine kyiv city",
    "romania": "romania bucharest city",
    "hungary": "hungary budapest city",
    "czech": "prague czech city",
    "sweden": "sweden stockholm city",
    "norway": "norway fjord nature",
    "denmark": "denmark copenhagen city",
    "finland": "finland helsinki city",
    "switzerland": "switzerland alpine mountain",
    "austria": "austria vienna city",
    "bali": "bali indonesia beach",
    "japan": "japan tokyo street",
    "dubai": "dubai skyline luxury",
    "germany": "germany berlin city",
    "india": "india city street",
    "portugal": "portugal lisbon city",
    "canada": "canada city nature",
    "australia": "australia sydney city",
    "singapore": "singapore skyline city",
    "taiwan": "taiwan taipei city night",
    "hongkong": "hong kong skyline night",
    "hong kong": "hong kong skyline night",
    "korea": "south korea seoul cityscape",
    "china": "china beijing skyline",
    "russia": "russia moscow city",
    "georgia": "georgia tbilisi old town",
    "albania": "albania tirana city",
    "serbia": "serbia belgrade city",
    "croatia": "croatia dubrovnik coast",
    "estonia": "estonia tallinn old town",
    "latvia": "latvia riga city",
    "lithuania": "lithuania vilnius city",
    "slovakia": "slovakia bratislava city",
    "bulgaria": "bulgaria sofia city",
    "usa": "new york city usa",
    "uk": "london uk city",
    # ── Cities ──────────────────────────────────────────────────────────────
    "new york": "new york city manhattan",
    "london": "london uk city",
    "paris": "paris eiffel tower",
    "tokyo": "tokyo japan street",
    "berlin": "berlin germany city",
    "amsterdam": "amsterdam canal city",
    "barcelona": "barcelona spain city",
    "lisbon": "lisbon portugal city",
    "bangkok": "bangkok thailand street",
    "seoul": "seoul south korea city",
    "hanoi": "hanoi vietnam street",
    "ho chi minh": "ho chi minh vietnam city",
    "mumbai": "mumbai india city",
    "cape town": "cape town south africa",
    "istanbul": "istanbul turkey bosphorus",
    "miami": "miami beach city",
    "los angeles": "los angeles california city",
    "toronto": "toronto canada skyline",
    "melbourne": "melbourne australia city",
    "sydney": "sydney australia opera house",
}


def _log(log_handler, message: str) -> None:
    if log_handler:
        log_handler(message)
    else:
        print(message)


def _extract_search_keywords(topic: str) -> str:
    """
    Extract the most relevant 2-3 keywords from a topic string for Pexels search.
    Uses keyword mapping first, then falls back to stop-word filtering.
    """
    topic_lower = topic.lower()

    # Check multi-word mappings first (longest match wins)
    for key in sorted(_KEYWORD_MAP.keys(), key=len, reverse=True):
        if key in topic_lower:
            return _KEYWORD_MAP[key]

    # Strip punctuation, split, filter stop words
    words = re.sub(r"[^a-z0-9 ]", " ", topic_lower).split()
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) > 2]

    # Return top 3 most meaningful words
    return " ".join(keywords[:3]) if keywords else topic.split()[0]


# ── Memory budget: maximum clip dimensions to download ─────────────────────────
# 4K clips (2160×3840) are 50-80 MB each and cause OOM on Render free tier.
# We want the SMALLEST clip that fills 720×1280 — that's 720p portrait (720×1280).
# Allow up to 1080p (1080×1920) as fallback if 720p unavailable.
_MAX_CLIP_HEIGHT  = 1440   # reject anything taller than this (rejects 4K/UHD)
_MAX_CLIP_SIZE_MB = 10     # hard cap: abort download after this many MB


def _pick_mp4_link(video_files: list[dict], log_handler=None) -> str | None:
    """
    Strict MP4 selection from a Pexels video_files array.

    Memory-safe priority order (smallest usable clip wins):
        1. Portrait mp4, height 720-1440  (720p-1080p — ideal for 720×1280 output)
        2. Portrait mp4, any height       (last resort before giving up)
        3. Any mp4                         (orientation fallback)

    4K / UHD clips (height > 1440) are explicitly rejected — they are 50-80 MB
    each and will OOM the Render free tier during FFmpeg decode.
    """
    if not video_files:
        return None

    # ── Keep only true video/mp4 files with a non-empty link ─────────────────
    mp4_files = [
        f for f in video_files
        if isinstance(f, dict)
        and (f.get("file_type") or "").strip().lower() in ("video/mp4", "mp4")
        and (f.get("link") or "").strip()
    ]

    if not mp4_files:
        types_seen = list({f.get("file_type") for f in video_files if isinstance(f, dict)})
        _log(log_handler,
             f"  [pexels] no mp4 ({len(video_files)} entries, types: {types_seen})")
        return None

    # ── Split into portrait vs landscape ─────────────────────────────────────
    portrait = [
        f for f in mp4_files
        if (f.get("height") or 0) >= (f.get("width") or 0)
    ]
    pool = portrait or mp4_files

    # ── Prefer 720p-1080p portrait; reject 4K (height > _MAX_CLIP_HEIGHT) ────
    preferred = [
        f for f in pool
        if 720 <= (f.get("height") or 0) <= _MAX_CLIP_HEIGHT
    ]
    # Fall back to any portrait/mp4 if no clip fits the preferred range
    candidates = preferred or pool

    # ── Among candidates, pick the LOWEST resolution that still meets 720p ───
    # Sorting ascending (smallest first) ensures we grab the lightest clip.
    # Less data downloaded, less FFmpeg decode memory, faster pipeline.
    candidates.sort(
        key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
    )

    chosen = candidates[0]
    link = (chosen.get("link") or "").strip()
    if not link:
        _log(log_handler, "  [pexels] chosen entry has empty link — skipping")
        return None

    quality = chosen.get("quality") or "unknown"
    width   = chosen.get("width")   or "?"
    height  = chosen.get("height")  or "?"
    _log(log_handler,
         f"  [pexels] mp4 selected: quality={quality} {width}x{height} -> {link[:80]}")
    return link


def _download_video(url: str, output_path: Path, log_handler=None) -> None:
    """
    Stream-download a video URL to output_path with a hard size cap.

    Hard limit: _MAX_CLIP_SIZE_MB (10 MB by default).
    If the response body exceeds this, the download is aborted and the
    partial file is deleted.  This prevents a 80 MB 4K clip from eating
    RAM on Render's free tier even if _pick_mp4_link accidentally selected
    a large file.

    Raises ValueError if the size cap is hit (caller will try the next video).
    Raises requests.HTTPError / requests.Timeout on network failure.
    """
    _log(log_handler, f"  [download] {url[:100]}")
    if not url.lower().split("?")[0].endswith((".mp4", ".mov", ".webm")):
        _log(log_handler, f"  [download] WARNING: URL may not be a video file: {url[:80]}")

    max_bytes = _MAX_CLIP_SIZE_MB * 1024 * 1024
    bytes_written = 0

    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "image" in content_type:
                raise ValueError(
                    f"Server returned an image ({content_type}) instead of video."
                )
            with output_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=512 * 1024):  # 512 KB chunks
                    if chunk:
                        bytes_written += len(chunk)
                        if bytes_written > max_bytes:
                            # Abort — clip is too large for our memory budget
                            raise ValueError(
                                f"Clip exceeds {_MAX_CLIP_SIZE_MB} MB limit "
                                f"({bytes_written // (1024*1024)} MB so far) — skipping"
                            )
                        fh.write(chunk)
    except ValueError:
        # Clean up partial file before re-raising so caller can try next video
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    size_kb = output_path.stat().st_size // 1024
    _log(log_handler, f"  [download] saved {output_path.name} ({size_kb} KB)")


def _create_placeholder_clips(count: int) -> list[str]:
    colors = [(15, 118, 110), (249, 115, 22), (30, 41, 59), (59, 130, 246), (22, 163, 74)]
    clip_paths: list[str] = []
    for i in range(count):
        file_name = f"placeholder_{i + 1}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
        output_path = VIDEOS_DIR / file_name
        clip = ColorClip(size=(1080, 1920), color=colors[i % len(colors)], duration=3)
        try:
            clip.write_videofile(
                output_path.as_posix(),
                fps=24,
                codec="libx264",
                audio=False,
                preset="ultrafast",
                logger=None,
            )
        finally:
            clip.close()
        clip_paths.append(to_storage_relative(output_path))
    return clip_paths


def fetch_video_clips(topic: str, log_handler=None, count: int = 4) -> list[str]:
    ensure_storage_dirs()
    count = min(max(count, 1), 15)
    pexels_api_key = os.getenv("PEXELS_API_KEY")

    # Check cache
    cache_key = (topic.lower(), count)
    cached = MEDIA_CACHE.get(cache_key, [])
    valid_cached = [p for p in cached if (VIDEOS_DIR.parent.parent / Path(p)).exists()]
    if len(valid_cached) >= count:
        _log(log_handler, "Using cached media clips")
        return valid_cached[:count]

    if pexels_api_key:
        try:
            kw = _extract_search_keywords(topic)
            search_query = f"{kw} aerial drone cinematic vertical".strip()
            _log(log_handler, f"Pexels video search: '{search_query}'")

            response = requests.get(
                "https://api.pexels.com/videos/search",   # videos endpoint, NOT /v1/search
                headers={"Authorization": pexels_api_key},
                params={
                    "query": search_query,
                    "per_page": max(15, count),
                    "orientation": "portrait",
                    "size": "large",
                },
                timeout=(10, 30),
            )
            response.raise_for_status()
            data = response.json()
            videos = data.get("videos", [])
            _log(log_handler, f"Pexels returned {len(videos)} video results")

            downloaded: list[str] = []
            for i, video in enumerate(videos):
                if len(downloaded) >= count:
                    break
                video_files = video.get("video_files", [])
                link = _pick_mp4_link(video_files, log_handler)
                if not link:
                    continue
                file_name = f"clip_{i + 1}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
                output_path = VIDEOS_DIR / file_name
                try:
                    _download_video(link, output_path, log_handler)
                    downloaded.append(to_storage_relative(output_path))
                    _log(log_handler, f"Clip {len(downloaded)}/{count} saved: {file_name}")
                except Exception as dl_exc:
                    _log(log_handler, f"Clip {i + 1} download failed: {dl_exc}")

            if downloaded:
                MEDIA_CACHE[cache_key] = downloaded[:]
                _log(log_handler, f"Pexels: {len(downloaded)} mp4 clips ready")
                return downloaded

        except Exception as exc:
            _log(log_handler, f"Pexels fetch failed: {exc}")

    _log(log_handler, "Using placeholder video clips (Pexels unavailable)")
    placeholder_paths = _create_placeholder_clips(count)
    MEDIA_CACHE[cache_key] = placeholder_paths[:]
    return placeholder_paths


# Keywords that indicate indoor/people-focused content where
# "aerial drone" would return no relevant results.
_INDOOR_KEYWORDS = {
    "kids", "child", "children", "baby", "toy", "toys", "classroom",
    "school", "office", "desk", "indoor", "kitchen", "cooking", "food",
    "gym", "workout", "face", "person", "people", "portrait", "selfie",
    "interview", "meeting", "studio", "dance", "fashion", "makeup",
    "hair", "beauty", "shopping", "mall", "restaurant", "cafe",
}


def _aerial_suffix(query: str) -> str:
    """
    Suffix appended to every Pexels scene query.

    For outdoor / landscape scenes: appends "aerial drone cinematic vertical"
    which surfaces B-roll-style footage ideal for travel/lifestyle reels.

    For indoor / people-focused scenes: "aerial drone" returns irrelevant
    outdoor shots.  For those queries we use "cinematic vertical" only so
    Pexels returns the most visually relevant indoor footage.

    Previously used '4k aerial drone cinematic vertical' which forced Pexels
    to return 4K UHD clips (50-80 MB each).  Removing '4k' lets Pexels serve
    HD/720p clips (3-10 MB) which are perfectly fine for a 720×1280 output.
    """
    query_lower = query.lower()
    if any(kw in query_lower for kw in _INDOOR_KEYWORDS):
        return "cinematic vertical"
    return "aerial drone cinematic vertical"


def _fetch_one_clip(
    query: str,
    scene_idx: int,
    log_handler=None,
    used_ids: set | None = None,
) -> str | None:
    """
    Fetch exactly one HD MP4 from Pexels for a single scene query.

    Tries up to two passes:
      Pass 1 — query + aerial/cinematic suffix (context-aware)
      Pass 2 — bare query only (fallback when aerial returns 0 results)

    `used_ids` is a shared set of already-downloaded Pexels video IDs.
    Videos whose ID is in `used_ids` are skipped so each scene gets a
    visually distinct clip.  The set is mutated in-place on success.

    Returns a storage-relative path on success, None on failure.
    """
    pexels_api_key = os.getenv("PEXELS_API_KEY")
    if not pexels_api_key:
        _log(log_handler, "Scene fetch skipped: PEXELS_API_KEY not set")
        return None

    suffix = _aerial_suffix(query)
    search_queries = [
        f"{query} {suffix}".strip(),   # pass 1: with aerial/cinematic suffix
        query.strip(),                  # pass 2: bare query only
    ]
    # De-duplicate (suffix already absent for indoor queries)
    if search_queries[0] == search_queries[1]:
        search_queries = [search_queries[0]]

    label = f"Scene {scene_idx + 1}"

    for attempt, search_query in enumerate(search_queries, start=1):
        _log(log_handler, f"{label} pass {attempt}: '{search_query}'")
        try:
            response = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": pexels_api_key},
                params={
                    "query": search_query,
                    "per_page": 12,          # fetch more results so dedup has room
                    "orientation": "portrait",
                    "size": "large",
                },
                timeout=(10, 30),
            )
            response.raise_for_status()
            videos = response.json().get("videos", [])
            _log(log_handler, f"{label}: {len(videos)} results")

            for video in videos:
                video_id = video.get("id")

                # Skip already-used video IDs to prevent repeated clips
                if used_ids is not None and video_id in used_ids:
                    _log(log_handler, f"{label}: skipping video {video_id} (already used)")
                    continue

                video_files = video.get("video_files", [])
                link = _pick_mp4_link(video_files, log_handler)
                if not link:
                    continue

                file_name = (
                    f"scene_{scene_idx + 1}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
                )
                output_path = VIDEOS_DIR / file_name
                try:
                    _download_video(link, output_path, log_handler)
                    # Mark this video as used before returning
                    if used_ids is not None and video_id:
                        used_ids.add(video_id)
                    _log(log_handler,
                         f"{label} OK — {file_name} "
                         f"({output_path.stat().st_size // 1024} KB)")
                    return to_storage_relative(output_path)
                except Exception as dl_exc:
                    _log(log_handler, f"{label} download error: {dl_exc}")

        except Exception as exc:
            _log(log_handler, f"{label} Pexels API error (pass {attempt}): {exc}")

    _log(log_handler, f"{label}: no mp4 found for '{query}'")
    return None


def fetch_scene_clips(scenes: list[dict], log_handler=None) -> list[str]:
    """
    Fetch exactly ONE portrait HD video per scene's search_query.

    Video ID deduplication is enforced across all scenes — each Pexels
    video can appear at most once in the reel, guaranteeing visual variety.

    Returns a list of clip paths, one per scene (placeholder used as fallback).
    """
    ensure_storage_dirs()
    clip_paths: list[str] = []
    used_ids: set[int] = set()   # shared across scenes to prevent duplicate clips

    for i, scene in enumerate(scenes):
        query = scene.get("search_query", "cinematic aesthetic")
        path = _fetch_one_clip(query, i, log_handler, used_ids=used_ids)
        if path:
            clip_paths.append(path)
        else:
            _log(log_handler, f"Scene {i + 1}: placeholder (no unique results for '{query}')")
            placeholder = _create_placeholder_clips(1)
            clip_paths.extend(placeholder)

    _log(log_handler, f"Fetched {len(clip_paths)} scene clips ({len(used_ids)} unique Pexels IDs)")
    return clip_paths
