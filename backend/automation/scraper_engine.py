"""
Scraper Engine — Apify-powered trending reel metadata extraction.

Workflow
────────
1. POST to Apify actor (instagram-scraper) with target hashtags
2. Poll for run completion (or use synchronous run-and-get endpoint)
3. Parse raw results → normalised TrendingReel objects
4. Rank by view count, return top N with hooks + audio URLs

Why Apify over direct scraping
──────────────────────────────
Instagram blocks headless browsers and rotating proxies within minutes.
Apify manages residential proxy rotation, browser fingerprinting, and
anti-bot evasion at scale — we just call a REST API.

Required env vars
─────────────────
APIFY_API_KEY=apify_api_xxxxx
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

APIFY_BASE = "https://api.apify.com/v2"

# Apify actor that scrapes Instagram hashtags and returns reel metadata.
# Actor store: https://apify.com/apify/instagram-scraper
_ACTOR_ID = "apify~instagram-scraper"

# Maximum seconds to wait for the Apify run to finish.
_RUN_TIMEOUT_S = 90
_POLL_INTERVAL_S = 3


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TrendingReel:
    url: str
    caption: str
    views: int
    likes: int
    audio_url: str | None      # direct MP3/M4A URL for yt-dlp
    audio_title: str | None    # song / audio name shown on IG
    hook: str                  # first sentence of caption (≤ 120 chars)
    hashtags: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


def _extract_hook(caption: str) -> str:
    """Return the first sentence of a caption, capped at 120 chars."""
    if not caption:
        return ""
    for sep in (".", "!", "?", "\n"):
        idx = caption.find(sep)
        if 0 < idx < 120:
            return caption[: idx + 1].strip()
    return caption[:120].strip()


def _extract_hashtags(caption: str) -> list[str]:
    import re
    return re.findall(r"#\w+", caption)


def _parse_item(item: dict) -> TrendingReel | None:
    """
    Normalise a raw Apify result item into a TrendingReel.

    Apify's instagram-scraper can return slightly different schemas
    depending on post type; we handle multiple key names defensively.
    """
    # Only process video / reel content
    type_field = (item.get("type") or item.get("mediaType") or "").lower()
    if type_field not in {"video", "reel", "clips", ""}:
        return None

    url = (
        item.get("url")
        or item.get("shortCode") and f"https://www.instagram.com/reel/{item['shortCode']}/"
        or ""
    )
    if not url:
        return None

    caption = item.get("caption") or item.get("text") or ""
    views = int(
        item.get("videoViewCount")
        or item.get("viewsCount")
        or item.get("videoPlayCount")
        or item.get("playsCount")
        or 0
    )
    likes = int(item.get("likesCount") or item.get("likes") or 0)

    # Audio URL — IG exposes a direct video URL that contains the original audio
    audio_url = (
        item.get("videoUrl")
        or item.get("displayUrl")
        or None
    )

    # Music metadata if present
    music_info = item.get("musicInfo") or item.get("clips_metadata") or {}
    audio_title = (
        music_info.get("song_name")
        or music_info.get("music_canonical_id")
        or item.get("musicTitle")
        or None
    )

    thumbnail = item.get("displayUrl") or item.get("thumbnailUrl") or None

    return TrendingReel(
        url=url,
        caption=caption,
        views=views,
        likes=likes,
        audio_url=audio_url,
        audio_title=audio_title,
        hook=_extract_hook(caption),
        hashtags=_extract_hashtags(caption),
        thumbnail_url=thumbnail,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Apify run + poll
# ─────────────────────────────────────────────────────────────────────────────

def _apify_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _start_run(hashtags: list[str], count: int, api_key: str) -> str:
    """Start an Apify actor run and return the run ID."""
    payload = {
        "hashtags": hashtags,
        "resultsLimit": count,
        "resultsType": "posts",          # includes reels
        "addParentData": False,
        "proxy": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
        },
    }
    r = requests.post(
        f"{APIFY_BASE}/acts/{_ACTOR_ID}/runs",
        headers=_apify_headers(api_key),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["data"]["id"]


def _wait_for_run(run_id: str, api_key: str, log_handler=None) -> str:
    """
    Poll run status until SUCCEEDED/FAILED or timeout.
    Returns the default dataset ID on success.
    """
    deadline = time.time() + _RUN_TIMEOUT_S
    while time.time() < deadline:
        r = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=_apify_headers(api_key),
            timeout=10,
        )
        r.raise_for_status()
        run_data = r.json()["data"]
        status = run_data.get("status", "")
        _log(log_handler, f"Apify run status: {status}")

        if status == "SUCCEEDED":
            return run_data["defaultDatasetId"]
        if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
            raise RuntimeError(f"Apify run {run_id} ended with status: {status}")
        time.sleep(_POLL_INTERVAL_S)

    raise TimeoutError(f"Apify run did not finish within {_RUN_TIMEOUT_S}s")


def _fetch_dataset(dataset_id: str, api_key: str, limit: int) -> list[dict]:
    r = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers=_apify_headers(api_key),
        params={"limit": limit, "format": "json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def scrape_trending_reels(
    hashtags: list[str],
    scrape_count: int = 50,
    top_n: int = 10,
    log_handler=None,
) -> list[TrendingReel]:
    """
    Scrape the top `top_n` trending reels from the given hashtags.

    Returns reels sorted by view count (highest first).
    Falls back to empty list on any error so the pipeline continues.
    """
    api_key = os.getenv("APIFY_API_KEY")
    if not api_key:
        _log(log_handler, "APIFY_API_KEY not set — scraping skipped, using template hooks")
        return []

    try:
        _log(log_handler, f"Starting Apify scrape for: {hashtags}")
        run_id = _start_run(hashtags, scrape_count, api_key)
        dataset_id = _wait_for_run(run_id, api_key, log_handler)
        raw_items = _fetch_dataset(dataset_id, api_key, scrape_count)

        reels: list[TrendingReel] = []
        for item in raw_items:
            parsed = _parse_item(item)
            if parsed and parsed.hook:
                reels.append(parsed)

        # Sort by views descending, return top N
        reels.sort(key=lambda r: r.views, reverse=True)
        top = reels[:top_n]
        _log(log_handler, f"Scraped {len(reels)} reels, selected top {len(top)}")
        return top

    except Exception as exc:
        _log(log_handler, f"Apify scrape failed: {exc} — continuing without trend data")
        return []


def extract_top_hooks(reels: list[TrendingReel], max_hooks: int = 10) -> list[str]:
    """Return a clean list of hook strings from the top reels."""
    hooks = []
    for reel in reels[:max_hooks]:
        if reel.hook and len(reel.hook) > 10:
            hooks.append(reel.hook)
    return hooks


def pick_best_audio(reels: list[TrendingReel]) -> str | None:
    """Return the audio URL from the highest-view reel that has one."""
    for reel in reels:
        if reel.audio_url:
            return reel.audio_url
    return None
