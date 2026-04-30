#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                  LOCAL RENDERER — High Quality Reel Generator               ║
╚══════════════════════════════════════════════════════════════════════════════╝

Runs LOCALLY (your machine, Colab, VPS) — never on Render.

This script:
  1. Fetches the script/scene data from the Render API (or local backend)
  2. Downloads UNIQUE cinematic clips from Pexels (per-scene, deduplicated)
  3. Generates natural TTS voice with edge-tts (Microsoft Neural)
  4. Builds word-synced ASS subtitles (no Whisper — timing from script)
  5. Renders final reel with FFmpeg: 720×1280 · 30fps · medium · 2M · AAC 128k
  6. Saves to output/reel_TIMESTAMP.mp4

Usage
─────
  # Option A: trigger script generation on API then render
  python local_renderer.py --api-url https://your-backend.onrender.com --topic "Best budget countries 2026"

  # Option B: render from an existing job_id
  python local_renderer.py --api-url https://your-backend.onrender.com --job-id <uuid>

  # Option C: run fully locally (start backend with uvicorn first)
  python local_renderer.py --api-url http://localhost:8000 --topic "Hidden gems Europe"

  # Option D: load from a saved export JSON file (offline render)
  python local_renderer.py --from-file export_job.json

Requirements
────────────
  pip install requests edge-tts  (for TTS)
  # FFmpeg must be on PATH: winget install ffmpeg / brew install ffmpeg / apt install ffmpeg
  # PEXELS_API_KEY must be set in .env or environment

Environment variables (from .env or shell):
  PEXELS_API_KEY   — required for clip downloads
  ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID — optional, uses edge-tts if absent
  API_URL          — default backend URL (overridden by --api-url flag)
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

# ── Load .env from the project root or backend directory ─────────────────────
for _env_dir in (Path(__file__).parent, Path(__file__).parent / "backend"):
    _env_file = _env_dir / ".env"
    if _env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(_env_file)
        break

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_API_URL   = os.getenv("API_URL", "http://localhost:8000")
PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY", "")
ELEVENLABS_KEY    = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE  = os.getenv("ELEVENLABS_VOICE_ID", "")

OUTPUT_DIR = Path("output")
TEMP_DIR   = Path(tempfile.gettempdir()) / "reel_local_renderer"

# ── FFmpeg quality settings ────────────────────────────────────────────────────
# These are the HIGH QUALITY settings used by the local renderer.
# No memory limit here — use the best settings for the best output.
FRAME_W   = 720
FRAME_H   = 1280
FPS       = 30
PRESET    = "medium"          # medium = good compression + quality
CRF       = "23"              # 23 = visually lossless for social media
VBITRATE  = "2M"              # 2 Mbit/s target
MAXRATE   = "3M"              # ceiling
BUFSIZE   = "3M"
ABITRATE  = "128k"            # AAC 128k — clear voice + music

# ── Caption style ─────────────────────────────────────────────────────────────
CAP_FONT      = "Arial Black"
CAP_SIZE      = 48
CAP_OUTLINE   = 2
CAP_SHADOW    = 1
CAP_MARGIN_V  = 80             # pixels from bottom edge
CAP_FADE_MS   = 150            # fade in/out duration in ms

# ── Pexels clip constraints ────────────────────────────────────────────────────
MAX_CLIP_SIZE_MB = 50          # local renderer can handle larger clips
MAX_CLIP_HEIGHT  = 2160        # allow up to 4K on local machine (FFmpeg scales it)
PEXELS_PER_PAGE  = 15          # more candidates for better dedup

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[LocalRenderer] {msg}", flush=True)


def _resolve_ffmpeg() -> str:
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    # Common Windows install locations
    username = os.environ.get("USERNAME", os.environ.get("USER", ""))
    for tmpl in [
        rf"C:\Users\{username}\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]:
        if Path(tmpl).exists():
            return tmpl
    raise RuntimeError(
        "FFmpeg not found on PATH.  Install with:\n"
        "  Windows: winget install ffmpeg\n"
        "  macOS:   brew install ffmpeg\n"
        "  Linux:   sudo apt install ffmpeg"
    )


def _run_ffmpeg(cmd: list[str], label: str = "FFmpeg", timeout: int = 600) -> bool:
    _log(f"  Running {label}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            _log(f"  {label} FAILED (rc={result.returncode})")
            if result.stderr:
                _log(f"  stderr: {result.stderr[-2000:]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        _log(f"  {label} timed out after {timeout}s")
        return False
    except Exception as exc:
        _log(f"  {label} error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Fetch job data from API
# ─────────────────────────────────────────────────────────────────────────────

def _trigger_job(api_url: str, topic: str) -> str:
    """POST /automation/generate and return job_id."""
    _log(f"Triggering script generation — topic={topic!r}")
    r = requests.post(
        f"{api_url}/automation/generate",
        json={"topic": topic},
        timeout=30,
    )
    r.raise_for_status()
    job_id = r.json()["job_id"]
    _log(f"Job created: {job_id}")
    return job_id


def _poll_job(api_url: str, job_id: str, max_wait_seconds: int = 120) -> dict:
    """Poll GET /jobs/{job_id} until the job reaches a terminal state."""
    _log(f"Polling job {job_id}...")
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        r = requests.get(f"{api_url}/jobs/{job_id}", timeout=15)
        if r.status_code == 404:
            _log("  Job not found — waiting for DB write...")
            time.sleep(3)
            continue
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "")
        logs = data.get("logs", [])
        if logs:
            _log(f"  [{status}] {logs[-1]}")
        if status in ("completed", "script_ready", "failed"):
            return data
        time.sleep(5)
    raise TimeoutError(f"Job {job_id} did not complete within {max_wait_seconds}s")


def _fetch_export(api_url: str, job_id: str) -> dict:
    """GET /export-job/{job_id} — returns structured script data."""
    _log(f"Fetching export data for job {job_id}...")
    r = requests.get(f"{api_url}/export-job/{job_id}", timeout=15)
    if r.status_code == 202:
        raise RuntimeError("Job still processing — poll again")
    r.raise_for_status()
    return r.json()


def fetch_job_data(api_url: str, topic: str | None, job_id: str | None) -> dict:
    """High-level: trigger or fetch a job and return export data."""
    if job_id is None:
        if not topic:
            raise ValueError("Either --topic or --job-id must be provided")
        job_id = _trigger_job(api_url, topic)

    # Poll until script is ready
    _poll_job(api_url, job_id, max_wait_seconds=180)
    return _fetch_export(api_url, job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Download unique clips from Pexels
# ─────────────────────────────────────────────────────────────────────────────

_INDOOR_KEYWORDS = {
    "kids", "child", "children", "baby", "classroom", "school", "office",
    "desk", "indoor", "kitchen", "cooking", "food", "gym", "workout",
    "face", "person", "people", "portrait", "selfie", "interview", "meeting",
    "studio", "dance", "fashion", "makeup", "hair", "beauty", "restaurant",
}


def _pick_best_mp4(video_files: list[dict]) -> str | None:
    """Pick the best portrait MP4 from a Pexels video_files array."""
    mp4 = [
        f for f in video_files
        if isinstance(f, dict)
        and (f.get("file_type") or "").lower() in ("video/mp4", "mp4")
        and (f.get("link") or "").strip()
    ]
    if not mp4:
        return None

    portrait = [f for f in mp4 if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or mp4

    # Sort by height descending — pick highest quality available (local machine can handle it)
    pool.sort(key=lambda f: (f.get("height") or 0), reverse=True)

    # But cap at a reasonable size to avoid absurdly slow downloads
    for f in pool:
        h = f.get("height") or 0
        if 720 <= h <= MAX_CLIP_HEIGHT:
            return (f.get("link") or "").strip() or None

    return (pool[0].get("link") or "").strip() or None


def _download_clip(url: str, dest: Path) -> bool:
    """Stream-download a video clip to dest with a size cap."""
    try:
        with requests.get(url, stream=True, timeout=(10, 120)) as r:
            r.raise_for_status()
            size = 0
            cap = MAX_CLIP_SIZE_MB * 1024 * 1024
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=512 * 1024):
                    fh.write(chunk)
                    size += len(chunk)
                    if size > cap:
                        fh.flush()
                        break
        return dest.exists() and dest.stat().st_size > 10_000
    except Exception as exc:
        _log(f"    Download failed: {exc}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def download_scene_clips(scenes: list[dict], clips_dir: Path) -> list[Path]:
    """
    Download one unique portrait clip per scene from Pexels.

    Uses used_ids set to prevent the same video appearing in multiple scenes.
    Falls back to a second search query if the first produces only duplicates.
    """
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY not set — cannot download clips")

    clips_dir.mkdir(parents=True, exist_ok=True)
    used_ids: set[int] = set()
    clip_paths: list[Path] = []

    for i, scene in enumerate(scenes):
        # Use visual_prompt (enriched) first, fall back to search_query
        queries = [
            scene.get("visual_prompt", ""),
            scene.get("search_query", ""),
            "cinematic aerial travel landscape",
        ]
        queries = [q for q in queries if q.strip()]

        found = False
        for attempt, query in enumerate(queries):
            _log(f"Scene {i+1}/{len(scenes)} — query={query!r} (attempt {attempt+1})")
            try:
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": PEXELS_API_KEY},
                    params={
                        "query": query,
                        "per_page": PEXELS_PER_PAGE,
                        "orientation": "portrait",
                        "size": "large",
                    },
                    timeout=(10, 30),
                )
                r.raise_for_status()
                videos = r.json().get("videos", [])
                _log(f"  {len(videos)} results")

                for video in videos:
                    vid_id = video.get("id")
                    if vid_id in used_ids:
                        continue
                    link = _pick_best_mp4(video.get("video_files", []))
                    if not link:
                        continue
                    dest = clips_dir / f"scene_{i+1:02d}_{datetime.utcnow().strftime('%f')}.mp4"
                    _log(f"  Downloading scene {i+1} (video_id={vid_id})...")
                    if _download_clip(link, dest):
                        size_mb = dest.stat().st_size / 1024 / 1024
                        _log(f"  ✓ Scene {i+1}: {dest.name} ({size_mb:.1f} MB)")
                        used_ids.add(vid_id)
                        clip_paths.append(dest)
                        found = True
                        break

                if found:
                    break

            except Exception as exc:
                _log(f"  Pexels error (attempt {attempt+1}): {exc}")

        if not found:
            _log(f"  Scene {i+1}: no unique clip found — using colour placeholder")
            # Create a 3s black placeholder via FFmpeg
            ffmpeg_bin = _resolve_ffmpeg()
            ph = clips_dir / f"scene_{i+1:02d}_placeholder.mp4"
            _run_ffmpeg([
                ffmpeg_bin, "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={FRAME_W}x{FRAME_H}:d=3:r={FPS}",
                "-c:v", "libx264", "-preset", "ultrafast",
                str(ph),
            ], label=f"Placeholder {i+1}")
            if ph.exists():
                clip_paths.append(ph)

    _log(f"Downloaded {len(clip_paths)}/{len(scenes)} clips ({len(used_ids)} unique Pexels IDs)")
    return clip_paths


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Generate TTS voice (edge-tts primary, ElevenLabs optional)
# ─────────────────────────────────────────────────────────────────────────────

async def _edge_tts(text: str, output_path: Path, voice: str = "en-IN-NeerjaNeural") -> bool:
    """Generate audio with edge-tts (Microsoft Neural, free, no key needed)."""
    try:
        import edge_tts
        tts = edge_tts.Communicate(text, voice=voice, rate="+5%", volume="+10%")
        await tts.save(str(output_path))
        return output_path.exists() and output_path.stat().st_size > 1000
    except Exception as exc:
        _log(f"  edge-tts error: {exc}")
        return False


def _elevenlabs_tts(text: str, output_path: Path) -> bool:
    """Generate audio with ElevenLabs (if API key is set)."""
    if not ELEVENLABS_KEY or not ELEVENLABS_VOICE:
        return False
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
            headers={
                "xi-api-key": ELEVENLABS_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=30,
        )
        if r.ok:
            output_path.write_bytes(r.content)
            return output_path.exists() and output_path.stat().st_size > 1000
    except Exception as exc:
        _log(f"  ElevenLabs error: {exc}")
    return False


def generate_voice(script_text: str, output_path: Path) -> Path:
    """Generate TTS audio. Returns path to the MP3 file."""
    _log("Generating TTS voice...")

    # Try ElevenLabs first (best quality) if key is configured
    if ELEVENLABS_KEY and ELEVENLABS_VOICE:
        _log("  Using ElevenLabs (eleven_multilingual_v2)...")
        if _elevenlabs_tts(script_text, output_path):
            size_kb = output_path.stat().st_size // 1024
            _log(f"  ✓ ElevenLabs audio: {output_path.name} ({size_kb} KB)")
            return output_path

    # edge-tts fallback (Indian English neural voice, great for Hinglish)
    _log("  Using edge-tts (en-IN-NeerjaNeural)...")
    ok = asyncio.run(_edge_tts(script_text, output_path))
    if ok:
        size_kb = output_path.stat().st_size // 1024
        _log(f"  ✓ edge-tts audio: {output_path.name} ({size_kb} KB)")
        return output_path

    raise RuntimeError("Both ElevenLabs and edge-tts failed to generate audio")


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — ASS subtitle generation (no Whisper — timing from script)
# ─────────────────────────────────────────────────────────────────────────────

_ASS_HEADER = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {FRAME_W}
PlayResY: {FRAME_H}
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{CAP_FONT},{CAP_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,{CAP_OUTLINE},{CAP_SHADOW},2,30,30,{CAP_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cc"""
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _group_words(words: list[tuple[float, float, str]], max_per_group: int = 3) -> list[tuple[float, float, str]]:
    """Group word-level stamps into caption phrases."""
    phrases: list[tuple[float, float, str]] = []
    buf: list[tuple[float, float, str]] = []
    for stamp in words:
        buf.append(stamp)
        if len(buf) >= max_per_group:
            start = buf[0][0]
            end   = buf[-1][1]
            text  = " ".join(w[2] for w in buf)
            phrases.append((start, end, text))
            buf = []
    if buf:
        start = buf[0][0]
        end   = buf[-1][1]
        text  = " ".join(w[2] for w in buf)
        phrases.append((start, end, text))
    return phrases


def _estimate_word_timestamps(
    script_text: str,
    audio_duration: float,
    hook_duration: float = 0.0,
) -> list[tuple[float, float, str]]:
    """
    Distribute script words evenly across audio duration — no Whisper needed.

    The hook duration is skipped (no captions on the visual hook screen).
    Remaining duration is split proportionally across all words.
    """
    # Clean text — remove punctuation except apostrophes
    clean = re.sub(r"[^\w\s']", " ", script_text)
    words = [w for w in clean.split() if w.strip()]
    if not words:
        return []

    caption_start = hook_duration
    caption_dur   = max(audio_duration - hook_duration - 0.5, 1.0)
    word_dur      = caption_dur / len(words)

    stamps: list[tuple[float, float, str]] = []
    for i, word in enumerate(words):
        start = caption_start + i * word_dur
        end   = start + word_dur
        stamps.append((round(start, 3), round(end, 3), word))
    return stamps


def _highlight_phrase(text: str) -> str:
    """Yellow highlight on the longest word — cinematic style."""
    words = text.upper().split()
    if not words:
        return text.upper()
    longest_idx = max(range(len(words)), key=lambda i: len(words[i]))
    result = []
    for i, w in enumerate(words):
        if i == longest_idx:
            result.append(f"{{\\c&H0000FFFF&}}{w}{{\\c}}")
        else:
            result.append(w)
    return " ".join(result)


def generate_ass_subtitles(
    script_text: str,
    audio_duration: float,
    output_path: Path,
    hook_duration: float = 0.0,
) -> Path:
    """
    Generate an ASS subtitle file with bottom-aligned, fade-animated captions.

    Styling:
      • Font: Arial Black, size 48
      • Outline: 2px black, Shadow: 1px
      • Alignment: 2 (bottom-center)
      • Fade: 150ms in / 150ms out
      • Yellow highlight on the longest word per phrase
    """
    stamps = _estimate_word_timestamps(script_text, audio_duration, hook_duration)
    if not stamps:
        _log("  No timestamps for subtitles")
        output_path.write_text(_ASS_HEADER, encoding="utf-8")
        return output_path

    phrases = _group_words(stamps, max_per_group=3)

    lines = [_ASS_HEADER]
    fade_tag = f"{{\\fad({CAP_FADE_MS},{CAP_FADE_MS})}}"
    for start, end, text in phrases:
        end = max(end, start + 0.40)
        highlighted = _highlight_phrase(text)
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{fade_tag}{highlighted}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    _log(f"  ✓ ASS subtitles: {len(phrases)} phrases → {output_path.name}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Step 5-7 — FFmpeg render pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _get_audio_duration(audio_path: Path) -> float:
    ffmpeg_bin = _resolve_ffmpeg()
    ffprobe = str(Path(ffmpeg_bin).with_name(
        "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ))
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 20.0


def _escape_filter_path(path: Path) -> str:
    """Escape a file path for use inside an FFmpeg filter_complex string."""
    p = str(path.absolute()).replace("\\", "/")
    p = p.replace(":", "\\:")
    return p


def render_reel(
    clip_paths: list[Path],
    audio_path: Path,
    ass_path: Path,
    output_path: Path,
    scenes: list[dict],
    log_fn=None,
) -> Path:
    """
    High-quality FFmpeg render pipeline — runs entirely on local machine.

    Phase 1: Encode each source clip into a normalised 720×1280 segment.
             Uses medium preset + 2M bitrate for excellent quality.
             Scale → crop → set SAR → set FPS — all in one decode pass.

    Phase 2: Concat all segments + mix audio + burn ASS subtitles.
             Single combined filter_complex: video subtitle overlay + audio mix.

    Output quality:
        Resolution: 720×1280  (Instagram vertical)
        FPS:        30
        Codec:      H.264 (libx264), medium preset, CRF 23, 2 Mbit/s
        Audio:      AAC 128k, 44100 Hz stereo
        Subtitles:  ASS burned-in (yellow keyword highlight, bottom-aligned)
    """
    if log_fn is None:
        log_fn = _log

    ffmpeg_bin = _resolve_ffmpeg()
    audio_dur  = _get_audio_duration(audio_path)
    num_scenes = max(len(clip_paths), 1)
    scene_dur  = max(audio_dur / num_scenes, 2.0)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

    segment_paths: list[Path] = []
    concat_list   = TEMP_DIR / f"concat_{tag}.txt"

    try:
        # ── Phase 1: per-clip encoding ────────────────────────────────────────
        for idx, cp in enumerate(clip_paths):
            if not cp.exists():
                log_fn(f"  Clip {idx+1} missing — skipping")
                continue

            # Probe actual clip duration
            try:
                clip_dur_raw = subprocess.run(
                    [str(Path(ffmpeg_bin).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")),
                     "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(cp)],
                    capture_output=True, text=True, timeout=10,
                )
                clip_dur = float(clip_dur_raw.stdout.strip()) if clip_dur_raw.returncode == 0 else scene_dur
            except Exception:
                clip_dur = scene_dur

            target_dur = round(min(scene_dur, clip_dur), 3)
            target_dur = max(target_dur, 1.5)

            seg = TEMP_DIR / f"seg_{idx:02d}_{tag}.mp4"

            seg_cmd = [
                ffmpeg_bin, "-y",
                "-i", str(cp),
                "-vf", (
                    f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
                    f"crop={FRAME_W}:{FRAME_H},"
                    f"setsar=1,"
                    f"fps={FPS},"
                    f"trim=duration={target_dur:.3f},"
                    f"setpts=PTS-STARTPTS"
                ),
                "-c:v", "libx264", "-preset", PRESET,
                "-crf", CRF, "-b:v", VBITRATE,
                "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-an", "-r", str(FPS), "-t", str(target_dur),
                str(seg),
            ]

            log_fn(f"  Encoding segment {idx+1}/{num_scenes} ({target_dur:.1f}s)...")
            ok = _run_ffmpeg(seg_cmd, label=f"Seg {idx+1}", timeout=300)
            if ok and seg.exists():
                segment_paths.append(seg)
            else:
                log_fn(f"  Segment {idx+1} failed — skipping")

            gc.collect()

        if not segment_paths:
            raise RuntimeError("No segments produced — all clips failed to encode")

        # ── Phase 2: concat + audio + ASS subtitles ───────────────────────────
        with open(concat_list, "w") as fh:
            for seg in segment_paths:
                fh.write(f"file '{str(seg).replace(chr(92), '/')}'\n")

        # Audio filter: speed up slightly for punchy pacing + small delay
        afmt    = "aformat=sample_rates=44100:channel_layouts=stereo"
        a_chain = f"[1:a]{afmt},volume=1.0,adelay=300|300,atempo=1.15,apad[final_a]"

        # Video filter: burn ASS subtitles
        esc_ass  = _escape_filter_path(ass_path)
        v_chain  = f"[0:v]ass=filename='{esc_ass}'[final_v]"
        full_fc  = f"{v_chain};{a_chain}"

        # Total duration = audio duration / atempo + delay
        total_dur = (audio_dur / 1.15) + 0.3 + 0.5

        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", str(audio_path),
            "-filter_complex", full_fc,
            "-map", "[final_v]", "-map", "[final_a]",
            "-c:v", "libx264", "-preset", PRESET,
            "-profile:v", "high", "-level:v", "4.0",
            "-crf", CRF, "-b:v", VBITRATE,
            "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
            "-c:a", "aac", "-b:a", ABITRATE, "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-movflags", "+faststart",
            "-shortest", "-t", str(total_dur),
            str(output_path),
        ]

        log_fn("  Merging segments + audio + subtitles → final reel...")
        ok = _run_ffmpeg(concat_cmd, label="Final concat", timeout=600)
        if not ok:
            raise RuntimeError("FFmpeg final concat failed — check logs above")

        log_fn(f"  ✓ Reel rendered: {output_path}")
        return output_path

    finally:
        # Clean up temp segments and concat list
        for seg in segment_paths:
            try:
                seg.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            concat_list.unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run(
    api_url: str,
    topic: str | None,
    job_id: str | None,
    from_file: str | None,
) -> Path:
    """Full end-to-end local render pipeline."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    work_dir  = TEMP_DIR / f"job_{timestamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Get job data ──────────────────────────────────────────────────
    if from_file:
        _log(f"Loading job data from file: {from_file}")
        export_data = json.loads(Path(from_file).read_text(encoding="utf-8"))
    else:
        export_data = fetch_job_data(api_url, topic, job_id)

    topic_out  = export_data.get("topic", topic or "reel")
    scenes     = export_data.get("scenes", [])
    script     = export_data.get("script", "\n".join(s.get("text", "") for s in scenes))
    format_type = export_data.get("format_type", "voiceover")

    _log(f"Topic: {topic_out!r}")
    _log(f"Scenes: {len(scenes)}")
    _log(f"Format: {format_type}")

    if not scenes:
        raise RuntimeError("No scenes in export data — cannot render")

    # Save export JSON for reference
    export_file = work_dir / "export.json"
    export_file.write_text(json.dumps(export_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Step 2: Download clips ────────────────────────────────────────────────
    clips_dir  = work_dir / "clips"
    clip_paths = download_scene_clips(scenes, clips_dir)

    if not clip_paths:
        raise RuntimeError("No clips downloaded — check PEXELS_API_KEY and connectivity")

    gc.collect()

    # ── Step 3: Generate TTS voice ────────────────────────────────────────────
    audio_path = work_dir / "voice.mp3"
    generate_voice(script, audio_path)
    audio_dur  = _get_audio_duration(audio_path)
    _log(f"Audio duration: {audio_dur:.1f}s")

    gc.collect()

    # ── Step 4: Generate ASS subtitles ───────────────────────────────────────
    ass_path = work_dir / "captions.ass"
    generate_ass_subtitles(
        script_text=script,
        audio_duration=audio_dur,
        output_path=ass_path,
        hook_duration=0.0,   # no hook screen in local renderer
    )

    # ── Step 5-7: FFmpeg render ───────────────────────────────────────────────
    output_name = f"reel_{timestamp}_{re.sub(r'[^a-z0-9]', '_', topic_out.lower())[:30]}.mp4"
    output_path = OUTPUT_DIR / output_name

    _log("Starting FFmpeg render pipeline...")
    render_reel(
        clip_paths=clip_paths,
        audio_path=audio_path,
        ass_path=ass_path,
        output_path=output_path,
        scenes=scenes,
    )

    # Verify output
    if not output_path.exists():
        raise RuntimeError(f"Render completed but output file not found: {output_path}")

    size_mb = output_path.stat().st_size / 1024 / 1024
    _log(f"\n{'='*60}")
    _log(f"✅ REEL READY: {output_path}")
    _log(f"   Size: {size_mb:.1f} MB")
    _log(f"   Topic: {topic_out}")
    _log(f"   Scenes: {len(clip_paths)}")
    _log(f"   Resolution: {FRAME_W}×{FRAME_H} @ {FPS}fps")
    _log(f"   Quality: {PRESET} preset, {CRF} CRF, {VBITRATE} bitrate")
    _log(f"{'='*60}\n")

    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Local high-quality reel renderer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--api-url",   default=DEFAULT_API_URL,
                        help="Backend API URL (default: %(default)s)")
    parser.add_argument("--topic",     default=None,
                        help="Topic to generate a new script for")
    parser.add_argument("--job-id",    default=None,
                        help="Existing job_id to render (skips script generation)")
    parser.add_argument("--from-file", default=None,
                        help="Load export JSON from file (offline mode)")
    parser.add_argument("--output-dir", default="output",
                        help="Where to save the reel (default: output/)")

    args = parser.parse_args()

    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.output_dir)

    if not args.topic and not args.job_id and not args.from_file:
        parser.error("Provide --topic, --job-id, or --from-file")

    try:
        output = run(
            api_url=args.api_url,
            topic=args.topic,
            job_id=args.job_id,
            from_file=args.from_file,
        )
        print(f"\nDone! Open your reel:\n  {output.absolute()}")
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
