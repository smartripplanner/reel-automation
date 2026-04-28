#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Render Build Script
# ─────────────────────────────────────────────────────────────────────────────
# Set this as the Build Command in Render dashboard:
#   bash build.sh
#
# Or in render.yaml:
#   buildCommand: bash build.sh
#
# Why preload Whisper here
# ───────────────────────
# Downloading the faster-whisper "tiny" model (~75 MB) during runtime would
# happen exactly when FFmpeg is also running — pushing Render free tier over
# 512 MB and triggering an OOM kill.  Downloading during the build phase is
# safe because build containers have no memory limit.
#
# The model is cached in storage/models/ which Render persists across deploys
# if you add a Render Disk at /opt/render/project/src/backend/storage/.
# Without a persistent disk the download runs each deploy — still better than
# during runtime since build memory is unlimited.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

echo "[Build] Installing Python dependencies..."
pip install -r requirements.txt

echo "[Build] Pre-downloading faster-whisper tiny model..."
python - <<'PYEOF'
import os, sys
from pathlib import Path

model_dir = Path(__file__).parent / "storage" / "models" if False else Path("storage/models")
model_dir.mkdir(parents=True, exist_ok=True)

try:
    from faster_whisper import WhisperModel
    print(f"[Build] Downloading faster-whisper tiny → {model_dir}")
    WhisperModel(
        "tiny",
        device="cpu",
        compute_type="int8",
        download_root=str(model_dir),
    )
    print("[Build] faster-whisper tiny model cached successfully")
except ImportError:
    print("[Build] faster-whisper not installed — skipping model download (captions use estimation fallback)")
except Exception as exc:
    print(f"[Build] WARNING: Whisper preload failed ({exc}) — captions will use estimation fallback")
    sys.exit(0)  # non-fatal: estimation-based captions still work
PYEOF

echo "[Build] Done."
