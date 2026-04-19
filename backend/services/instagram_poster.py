"""
Instagram Graph API Publisher
==============================
Publishes a rendered .mp4 reel (hosted on AWS S3) to Instagram as a Reel
using the Facebook Graph API v18.0.

Three-step flow
────────────────
Step 1 — Create container : POST /{IG_USER_ID}/media
          ▸ Tells Instagram to pull the video from the S3 URL.
          ▸ Returns a  creation_id  (asynchronous download begins server-side).

Step 2 — Poll status      : GET /{creation_id}?fields=status_code
          ▸ Instagram downloads & transcodes the 4K video from S3.
            This takes 30-120 s for typical 60-90 s 4K clips.
          ▸ Polls every 10 s until status_code == "FINISHED" (or error).

Step 3 — Publish          : POST /{IG_USER_ID}/media_publish
          ▸ Makes the container live on the feed.
          ▸ Returns the public Post ID.

Configuration (.env keys)
──────────────────────────
    IG_USER_ID     = 123456789          # numeric Instagram Business/Creator ID
    IG_ACCESS_TOKEN = EAAxx...          # long-lived page access token with
                                        # instagram_basic, instagram_content_publish,
                                        # pages_read_engagement permissions

Graceful degradation
─────────────────────
If either env var is missing, or if any API call fails, the function logs
a clear warning and returns None.  The pipeline continues normally —
a missing IG config must never crash reel generation.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_IG_USER_ID     = os.getenv("IG_USER_ID", "")
_IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
_GRAPH_API_BASE  = "https://graph.facebook.com/v18.0"

# Polling tuning — Instagram typically finishes in 30-90 s for 4K ~60 s clips.
# 10 s interval × 36 attempts = 6 minutes max before we give up.
_POLL_INTERVAL_SEC  = 10
_POLL_MAX_ATTEMPTS  = 36

# Status codes returned by the Graph API container status endpoint
_STATUS_FINISHED = "FINISHED"
_STATUS_ERROR    = "ERROR"
_STATUS_EXPIRED  = "EXPIRED"
_TERMINAL_STATUSES = {_STATUS_FINISHED, _STATUS_ERROR, _STATUS_EXPIRED}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_configured() -> bool:
    """Return True only when both required credentials are set."""
    return bool(_IG_USER_ID and _IG_ACCESS_TOKEN)


def _log(msg: str, handler: Optional[Callable[[str], None]] = None) -> None:
    """Emit to Python logger, stdout, and the optional pipeline log_handler."""
    logger.info(msg)
    print(msg)
    if handler:
        handler(msg)


def _extract_api_error(data: dict) -> str:
    """Pull a human-readable error message from a Graph API error envelope."""
    err = data.get("error", {})
    return err.get("message") or err.get("type") or str(err)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def upload_reel_to_instagram(
    video_url: str,
    caption: str,
    log_handler: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Publish a reel from a public S3 URL to Instagram.

    Parameters
    ----------
    video_url   : Fully-public HTTPS URL of the rendered .mp4 (from S3).
    caption     : Full caption string, including any CTA text already appended.
    log_handler : Optional pipeline log callback — same signature as the one
                  used throughout the main pipeline.

    Returns
    -------
    str  — the published Instagram Post ID (e.g. "17854360229135492").
    None — if credentials are absent, the video URL is empty, or any API
           step fails.  Returning None is intentional: the pipeline must not
           crash because of a missing or misconfigured IG setup.
    """

    # ── Pre-flight checks ─────────────────────────────────────────────────────
    if not _is_configured():
        _log(
            "[Instagram] IG_USER_ID or IG_ACCESS_TOKEN not set — skipping publish. "
            "Add both to .env to enable automatic Instagram posting.",
            log_handler,
        )
        return None

    if not video_url:
        _log("[Instagram] No video URL provided — skipping publish.", log_handler)
        return None

    # ── Step 1: Create media container ───────────────────────────────────────
    _log(f"[Instagram] Step 1/3 — Creating REELS container | url={video_url}", log_handler)

    try:
        container_resp = requests.post(
            f"{_GRAPH_API_BASE}/{_IG_USER_ID}/media",
            data={
                "video_url":    video_url,
                "caption":      caption,
                "media_type":   "REELS",
                "share_to_feed": "true",
                "access_token": _IG_ACCESS_TOKEN,
            },
            timeout=30,
        )
        container_resp.raise_for_status()
        container_data = container_resp.json()

    except requests.exceptions.Timeout:
        _log("[Instagram] Container creation timed out (30 s) — skipping publish.", log_handler)
        return None
    except requests.exceptions.RequestException as exc:
        _log(f"[Instagram] Container creation request failed: {exc}", log_handler)
        return None

    if "error" in container_data:
        _log(
            f"[Instagram] Container creation API error: {_extract_api_error(container_data)}",
            log_handler,
        )
        return None

    creation_id: str = container_data.get("id", "")
    if not creation_id:
        _log(
            f"[Instagram] Unexpected container response (no id): {container_data}",
            log_handler,
        )
        return None

    _log(f"[Instagram] Container created — creation_id={creation_id}", log_handler)

    # ── Step 2: Poll status until FINISHED ───────────────────────────────────
    _log(
        f"[Instagram] Step 2/3 — Polling container status "
        f"(every {_POLL_INTERVAL_SEC}s, max {_POLL_MAX_ATTEMPTS} attempts = "
        f"{_POLL_MAX_ATTEMPTS * _POLL_INTERVAL_SEC // 60} min timeout)",
        log_handler,
    )

    status_code: str = "UNKNOWN"

    for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
        time.sleep(_POLL_INTERVAL_SEC)

        try:
            poll_resp = requests.get(
                f"{_GRAPH_API_BASE}/{creation_id}",
                params={
                    "fields":       "status_code",
                    "access_token": _IG_ACCESS_TOKEN,
                },
                timeout=15,
            )
            poll_resp.raise_for_status()
            poll_data = poll_resp.json()

        except requests.exceptions.Timeout:
            _log(
                f"[Instagram] Poll #{attempt}/{_POLL_MAX_ATTEMPTS} timed out — retrying in {_POLL_INTERVAL_SEC}s",
                log_handler,
            )
            continue
        except requests.exceptions.RequestException as exc:
            _log(
                f"[Instagram] Poll #{attempt}/{_POLL_MAX_ATTEMPTS} request error: {exc} — retrying",
                log_handler,
            )
            continue

        if "error" in poll_data:
            _log(
                f"[Instagram] Poll #{attempt} API error: {_extract_api_error(poll_data)} — aborting",
                log_handler,
            )
            return None

        status_code = poll_data.get("status_code", "UNKNOWN")
        _log(
            f"[Instagram] Poll #{attempt}/{_POLL_MAX_ATTEMPTS} — status={status_code}",
            log_handler,
        )

        if status_code == _STATUS_FINISHED:
            _log("[Instagram] Container ready — proceeding to publish.", log_handler)
            break

        if status_code in (_STATUS_ERROR, _STATUS_EXPIRED):
            _log(
                f"[Instagram] Container processing failed with status='{status_code}' — aborting.",
                log_handler,
            )
            return None

        # IN_PROGRESS or other transient states → keep polling

    else:
        # Loop exhausted without break → timed out
        _log(
            f"[Instagram] Polling timed out after {_POLL_MAX_ATTEMPTS} attempts "
            f"(last status='{status_code}') — skipping publish.",
            log_handler,
        )
        return None

    # ── Step 3: Publish ───────────────────────────────────────────────────────
    _log(f"[Instagram] Step 3/3 — Publishing creation_id={creation_id}", log_handler)

    try:
        publish_resp = requests.post(
            f"{_GRAPH_API_BASE}/{_IG_USER_ID}/media_publish",
            data={
                "creation_id":  creation_id,
                "access_token": _IG_ACCESS_TOKEN,
            },
            timeout=30,
        )
        publish_resp.raise_for_status()
        publish_data = publish_resp.json()

    except requests.exceptions.Timeout:
        _log("[Instagram] Publish request timed out (30 s).", log_handler)
        return None
    except requests.exceptions.RequestException as exc:
        _log(f"[Instagram] Publish request failed: {exc}", log_handler)
        return None

    if "error" in publish_data:
        _log(
            f"[Instagram] Publish API error: {_extract_api_error(publish_data)}",
            log_handler,
        )
        return None

    post_id: str = publish_data.get("id", "")
    if post_id:
        _log(f"[Instagram] ✅ Reel published — Post ID: {post_id}", log_handler)
        _log(f"[Instagram] View at: https://www.instagram.com/reel/{post_id}/", log_handler)
    else:
        _log(
            f"[Instagram] Publish succeeded but no post ID returned: {publish_data}",
            log_handler,
        )

    return post_id or None
