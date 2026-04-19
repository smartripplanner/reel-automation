"""
Cloud Bridge — AWS S3 Video Uploader
=====================================
Uploads the final FFmpeg .mp4 reel to an S3-compatible bucket and returns
a public URL.  Called automatically by the pipeline after a successful render.

Configuration (add to .env):
    AWS_ACCESS_KEY_ID     = your_access_key
    AWS_SECRET_ACCESS_KEY = your_secret_key
    AWS_BUCKET_NAME       = your-bucket-name
    AWS_REGION            = ap-southeast-1   # optional, defaults to us-east-1
    AWS_S3_ENDPOINT_URL   = https://...      # optional, for non-AWS S3-compatible stores
                                             # (e.g. Cloudflare R2, MinIO, Backblaze B2)

Public URL format returned:
    https://<bucket>.s3.<region>.amazonaws.com/<key>
    — or —
    <AWS_S3_ENDPOINT_URL>/<bucket>/<key>   (when endpoint override is set)

Graceful degradation:
    If boto3 is not installed or AWS credentials are missing, the function
    logs a clear warning and returns None — the pipeline continues normally
    so a missing S3 config never crashes a reel generation run.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environment config
# ─────────────────────────────────────────────────────────────────────────────

_AWS_ACCESS_KEY    = os.getenv("AWS_ACCESS_KEY_ID", "")
_AWS_SECRET_KEY    = os.getenv("AWS_SECRET_ACCESS_KEY", "")
_AWS_BUCKET        = os.getenv("AWS_BUCKET_NAME", "")
_AWS_REGION        = os.getenv("AWS_REGION", "us-east-1")
_AWS_ENDPOINT_URL  = os.getenv("AWS_S3_ENDPOINT_URL", "")   # leave blank for standard AWS


def _is_configured() -> bool:
    """Return True only when all required credentials are present."""
    return bool(_AWS_ACCESS_KEY and _AWS_SECRET_KEY and _AWS_BUCKET)


def _build_public_url(bucket: str, key: str, region: str, endpoint_url: str) -> str:
    """Construct the public HTTPS URL for the uploaded object."""
    if endpoint_url:
        # S3-compatible endpoint (Cloudflare R2, MinIO, Backblaze B2, etc.)
        base = endpoint_url.rstrip("/")
        return f"{base}/{bucket}/{key}"
    # Standard AWS S3 path-style URL
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def upload_video_to_cloud(local_file_path: str) -> str | None:
    """
    Upload a local .mp4 file to S3 and return its public URL.

    Parameters
    ----------
    local_file_path : absolute or storage-relative path to the .mp4 reel.

    Returns
    -------
    str  — public HTTPS URL of the uploaded video, e.g.
           "https://my-bucket.s3.ap-southeast-1.amazonaws.com/reels/reel_20260414_090012.mp4"
    None — if boto3 is missing, credentials are not configured, or upload fails.
           Returning None is intentional: the pipeline must not crash because
           of a missing cloud config — the local file is always preserved.

    ACL
    ───
    The object is uploaded with public-read ACL so the URL is directly
    accessible without signing.  If your bucket blocks public ACLs (a common
    AWS security best-practice), remove the ACL parameter and use a CloudFront
    distribution or pre-signed URLs instead.
    """
    if not _is_configured():
        logger.warning(
            "[CloudStorage] AWS credentials not configured — skipping upload. "
            "Set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_BUCKET_NAME in .env"
        )
        return None

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        logger.warning(
            "[CloudStorage] boto3 not installed — run: pip install boto3>=1.34.0"
        )
        return None

    # Resolve to an absolute path
    path = Path(local_file_path)
    if not path.is_absolute():
        # Assume storage-relative paths are anchored to the backend directory
        backend_root = Path(__file__).parent
        path = backend_root / path

    if not path.exists():
        logger.error("[CloudStorage] File not found: %s", path)
        return None

    # Build a unique S3 key: reels/<date>/<filename>
    date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
    s3_key = f"reels/{date_prefix}/{path.name}"

    # Create the S3 client
    client_kwargs: dict = {
        "aws_access_key_id":     _AWS_ACCESS_KEY,
        "aws_secret_access_key": _AWS_SECRET_KEY,
        "region_name":           _AWS_REGION,
    }
    if _AWS_ENDPOINT_URL:
        client_kwargs["endpoint_url"] = _AWS_ENDPOINT_URL

    try:
        s3 = boto3.client("s3", **client_kwargs)

        logger.info("[CloudStorage] Uploading %s → s3://%s/%s", path.name, _AWS_BUCKET, s3_key)
        print(f"[CloudStorage] Uploading {path.name} → s3://{_AWS_BUCKET}/{s3_key}")

        s3.upload_file(
            Filename=str(path),
            Bucket=_AWS_BUCKET,
            Key=s3_key,
            ExtraArgs={
                "ACL":         "public-read",
                "ContentType": "video/mp4",
            },
        )

        public_url = _build_public_url(_AWS_BUCKET, s3_key, _AWS_REGION, _AWS_ENDPOINT_URL)
        logger.info("[CloudStorage] Upload complete: %s", public_url)
        print(f"[CloudStorage] Upload complete → {public_url}")
        return public_url

    except Exception as exc:
        logger.error("[CloudStorage] Upload failed: %s", exc)
        print(f"[CloudStorage] Upload failed: {exc}")
        return None
