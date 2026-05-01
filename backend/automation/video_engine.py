"""
Video Engine — Full-quality local FFmpeg renderer.

This runs ENTIRELY on the user's local machine with no memory constraints.
No FREE_PLAN mode. No quality downgrades. No memory guards.

Quality targets
───────────────
  Resolution : 720 × 1280 (portrait 9:16)   — upgrade to 1080p via env var
  FPS        : 30
  CRF        : 22  (visually near-lossless for web)
  Preset     : medium  (excellent quality / speed balance)
  Video rate : 2 Mbps target, 3 Mbps max
  Audio      : AAC 128 k, 44.1 kHz stereo
  Pixel fmt  : yuv420p  (maximum compatibility)
  Flags      : +faststart (instant web playback)

Render strategy
───────────────
Phase 1 — per-clip encode:
  Each source clip is individually transcoded to the target resolution,
  cropped to fill the 9:16 frame, and written as a small segment file.
  Source clips are deleted after encoding to free disk space.

Phase 2 — concat + audio + ASS burn:
  FFmpeg's concat demuxer joins all segments (stream-reads from disk — no
  simultaneous decode) while simultaneously mixing the TTS voice track,
  burning in ASS subtitles, and writing the final MP4 in a single pass.
"""

from __future__ import annotations

import gc
import os
import random
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from utils.pillow_compat import ensure_pillow_compat
from utils.storage import BASE_DIR, MUSIC_DIR, REELS_DIR, ensure_storage_dirs, to_storage_relative

ensure_pillow_compat()

# ─────────────────────────────────────────────────────────────────────────────
# Quality constants — LOCAL production mode, no limits
# ─────────────────────────────────────────────────────────────────────────────

# Allow 1080p via env: LOCAL_RENDER_1080=true
_WANT_1080 = os.getenv("LOCAL_RENDER_1080", "false").lower() in ("true", "1", "yes")

if _WANT_1080:
    FRAME_W, FRAME_H = 1080, 1920
    _VIDEO_BRATE = "4M"
    _MAX_RATE    = "6M"
    _BUF_SIZE    = "6M"
else:
    FRAME_W, FRAME_H = 720, 1280
    _VIDEO_BRATE = "2M"
    _MAX_RATE    = "3M"
    _BUF_SIZE    = "3M"

_PRESET      = "medium"     # best quality/speed trade-off for local rendering
_CRF         = "22"         # near-lossless visible quality
_AUDIO_BRATE = "128k"
FPS          = 30           # smooth 30fps for professional reels
HOOK_DURATION = 2.0
SAFE_X = int(FRAME_W * 0.07)
SAFE_Y = int(FRAME_H * 0.10)
MUSIC_VOLUME = 0.08
TEMP_RENDER_ROOT = BASE_DIR / "storage" / "tmp"
_SCENE_LIMIT = 7            # support up to 7 scenes

# ─────────────────────────────────────────────────────────────────────────────
# Hook screen styles
# ─────────────────────────────────────────────────────────────────────────────

STYLES = [
    {
        "name": "bold-news",
        "bg": (10, 15, 30),
        "text": (255, 255, 255),
        "accent": (251, 191, 36),
        "panel_alpha": 210,
        "hook_font_size": 96,
        "clip_duration": (2.0, 2.5),
        "zoom": 0.06,
    },
    {
        "name": "clean-lux",
        "bg": (2, 6, 23),
        "text": (248, 250, 252),
        "accent": (56, 189, 248),
        "panel_alpha": 185,
        "hook_font_size": 88,
        "clip_duration": (2.0, 2.6),
        "zoom": 0.05,
    },
    {
        "name": "creator-pop",
        "bg": (80, 20, 120),
        "text": (255, 255, 255),
        "accent": (249, 115, 22),
        "panel_alpha": 195,
        "hook_font_size": 100,
        "clip_duration": (1.8, 2.4),
        "zoom": 0.07,
    },
    {
        "name": "minimal-dark",
        "bg": (17, 24, 39),
        "text": (255, 255, 255),
        "accent": (34, 197, 94),
        "panel_alpha": 170,
        "hook_font_size": 84,
        "clip_duration": (2.2, 2.8),
        "zoom": 0.04,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Font loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/ariblk.ttf",       # Arial Black
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Linux
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",       # macOS
        "arialbd.ttf",
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────────────────────────────────────
# Hook screen — PNG rendered with PIL, fed to FFmpeg
# ─────────────────────────────────────────────────────────────────────────────

def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    dummy_img = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)
    for word in words:
        test = (current + " " + word).strip()
        bbox = dummy_draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def render_hook_frame(hook_text: str, style: dict, output_path: str) -> str:
    """Render a portrait PNG hook screen. Returns output_path."""
    bg = tuple(c - 20 for c in style["bg"]) if any(c > 20 for c in style["bg"]) else style["bg"]
    img = Image.new("RGB", (FRAME_W, FRAME_H), color=bg)
    draw = ImageDraw.Draw(img, "RGBA")

    max_text_w = int(FRAME_W * 0.84)
    clean_text = hook_text.replace("HOOK:", "").strip().upper()

    font_size = style["hook_font_size"]
    font = _load_font(font_size)
    lines = _wrap_text(clean_text, font, max_text_w)

    while len(lines) > 3 and font_size > 40:
        font_size -= 6
        font = _load_font(font_size)
        lines = _wrap_text(clean_text, font, max_text_w)

    line_h = font_size + 12
    block_h = len(lines) * line_h + 60
    block_w = min(max_text_w + 80, FRAME_W - SAFE_X * 2)
    bx = (FRAME_W - block_w) / 2
    by = (FRAME_H - block_h) / 2

    panel_color = (*style["bg"], style["panel_alpha"])
    draw.rounded_rectangle((bx, by, bx + block_w, by + block_h), radius=28, fill=panel_color)

    cy = by + 30
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        lw = bbox[2] - bbox[0]
        cx = (FRAME_W - lw) / 2
        draw.text((cx + 3, cy + 3), line, font=font, fill=(0, 0, 0, 140))
        draw.text((cx, cy), line, font=font, fill=style["text"], stroke_width=3,
                  stroke_fill=(0, 0, 0, 255))
        cy += line_h

    img.save(output_path, "PNG")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Music selection
# ─────────────────────────────────────────────────────────────────────────────

def _pick_music() -> str | None:
    if not MUSIC_DIR.exists():
        return None
    files = [p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}]
    return str(random.choice(files)) if files else None


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg binary resolution
# ─────────────────────────────────────────────────────────────────────────────

_FFMPEG_FALLBACK_PATHS = [
    r"C:\Users\{user}\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe",
    r"C:\Users\{user}\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-full_build\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\Users\{user}\scoop\apps\ffmpeg\current\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]


def _resolve_ffmpeg() -> str | None:
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    username = os.environ.get("USERNAME", os.environ.get("USER", ""))
    for template in _FFMPEG_FALLBACK_PATHS:
        candidate = template.replace("{user}", username)
        if Path(candidate).exists():
            return candidate
    return None


def _ffmpeg_available() -> bool:
    return _resolve_ffmpeg() is not None


def _get_media_duration(path: str) -> float:
    ffmpeg_bin = _resolve_ffmpeg() or "ffmpeg"
    ffprobe_bin = str(Path(ffmpeg_bin).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    target = os.path.abspath(path)
    probe_cmd = [
        ffprobe_bin, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        target,
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def _run_ffmpeg(
    cmd: list[str],
    log_handler=None,
    cwd: str | None = None,
    timeout: int = 1800,
    label: str = "FFmpeg",
) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            _log(log_handler, f"{label} failed (rc={result.returncode}): {stderr[-3000:]}")
            return False, stderr
        return True, stderr
    except subprocess.TimeoutExpired:
        msg = f"{label} timed out after {timeout}s"
        _log(log_handler, msg)
        return False, msg
    except Exception as exc:
        msg = f"{label} subprocess error: {exc}"
        _log(log_handler, msg)
        return False, msg


# ─────────────────────────────────────────────────────────────────────────────
# Path escaping for FFmpeg filter_complex
# ─────────────────────────────────────────────────────────────────────────────

def _escape_ass_path(path: str) -> str:
    """Escape a path for safe use inside FFmpeg filter_complex (colons, backslashes)."""
    p = str(Path(path).absolute()).replace("\\", "/")
    p = p.replace(":", "\\:")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — per-clip segment encoding
# Phase 2 — concat + audio + ASS burn → final MP4
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg_render(
    clip_paths: list[str],
    audio_path: str,
    output_path: str,
    total_duration: float,
    ass_path: str | None = None,
    log_handler=None,
) -> bool:
    """
    Two-phase FFmpeg render.

    Phase 1: Encode each clip individually → small uniform 720×1280 segment.
             Source clip deleted after encode (frees disk space immediately).

    Phase 2: concat demuxer joins segments → mix TTS audio → burn ASS → MP4.
             Single-pass re-encode with full quality settings.
    """
    if not clip_paths:
        _log(log_handler, "[Render] No clips provided")
        return False

    working_clips = clip_paths[:_SCENE_LIMIT]
    num_scenes = len(working_clips)
    scene_duration = max(total_duration / num_scenes, 2.0)

    ffmpeg_bin = _resolve_ffmpeg() or "ffmpeg"

    # Resolve audio path — may be absolute or relative to BASE_DIR
    audio_abs_candidate = Path(audio_path)
    if audio_abs_candidate.is_absolute() and audio_abs_candidate.exists():
        audio_abs = str(audio_abs_candidate)
    else:
        audio_abs = str(BASE_DIR / Path(audio_path))

    output_abs = os.path.abspath(output_path)

    TEMP_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
    job_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

    segment_paths: list[str] = []
    concat_list_path = str(TEMP_RENDER_ROOT / f"concat_{job_tag}.txt")

    try:
        # ── Phase 1: encode each clip → segment ────────────────────────────────
        for idx, cp in enumerate(working_clips):
            # Resolve clip path
            abs_cp_candidate = Path(cp)
            if abs_cp_candidate.is_absolute() and abs_cp_candidate.exists():
                abs_cp = str(abs_cp_candidate)
            else:
                abs_cp = str(BASE_DIR / Path(cp))

            if not Path(abs_cp).exists():
                _log(log_handler, f"[Render] Clip {idx+1} missing — skipping")
                continue

            source_dur = max(_get_media_duration(abs_cp), 0.1)
            target_dur = round(min(scene_duration, source_dur), 3)
            target_dur = max(target_dur, 1.5)

            seg_path = str(TEMP_RENDER_ROOT / f"seg_{idx}_{job_tag}.mp4")

            seg_cmd = [
                ffmpeg_bin, "-y",
                "-i", abs_cp,
                "-vf", (
                    f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
                    f"crop={FRAME_W}:{FRAME_H},"
                    f"setsar=1,"
                    f"fps={FPS},"
                    f"trim=duration={target_dur:.3f},"
                    f"setpts=PTS-STARTPTS"
                ),
                "-c:v", "libx264",
                "-preset", _PRESET,
                "-crf", _CRF,
                "-b:v", _VIDEO_BRATE,
                "-maxrate", _MAX_RATE,
                "-bufsize", _BUF_SIZE,
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-an",                      # no audio in segments
                "-r", str(FPS),
                "-t", str(target_dur),
                seg_path,
            ]

            _log(log_handler, f"[Render] Encoding segment {idx+1}/{num_scenes} ({target_dur:.1f}s)...")
            ok, _ = _run_ffmpeg(seg_cmd, log_handler=log_handler,
                                label=f"Seg {idx+1}", timeout=300)
            if ok and Path(seg_path).exists():
                segment_paths.append(seg_path)
            else:
                _log(log_handler, f"[Render] Segment {idx+1} failed — skipping clip")

            # Free disk space immediately
            try:
                Path(abs_cp).unlink(missing_ok=True)
            except Exception:
                pass

        if not segment_paths:
            _log(log_handler, "[Render] No segments produced — aborting")
            return False

        # ── Phase 2: write concat list ─────────────────────────────────────────
        with open(concat_list_path, "w") as fh:
            for seg in segment_paths:
                fh.write(f"file '{seg.replace(chr(92), '/')}'\n")

        # ── Phase 2: audio chain ───────────────────────────────────────────────
        # atempo=1.15 — 15% pitch-preserving speed-up for snappy reel pacing
        # adelay=500ms — half-second hook pause before voice starts
        afmt = "aformat=sample_rates=44100:channel_layouts=stereo"
        audio_chain = (
            f"[1:a]{afmt},volume=1.0,"
            f"adelay=500|500,"
            f"atempo=1.15,"
            f"apad[final_a]"
        )

        # ── Phase 2: subtitle burn-in ──────────────────────────────────────────
        use_ass = bool(ass_path and Path(ass_path).exists())

        if use_ass:
            escaped = _escape_ass_path(ass_path)
            video_chain = f"[0:v]ass=filename='{escaped}'[final_v]"
            full_filter = f"{video_chain};{audio_chain}"
            video_map = "[final_v]"
            _log(log_handler, "[Render] Burning ASS subtitles...")
        else:
            full_filter = audio_chain
            video_map = "0:v"
            _log(log_handler, "[Render] No subtitles — rendering video only")

        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-i", audio_abs,
            "-filter_complex", full_filter,
            "-map", video_map,
            "-map", "[final_a]",
            "-c:v", "libx264",
            "-preset", _PRESET,
            "-profile:v", "high",
            "-level:v", "4.1",
            "-crf", _CRF,
            "-b:v", _VIDEO_BRATE,
            "-maxrate", _MAX_RATE,
            "-bufsize", _BUF_SIZE,
            "-c:a", "aac",
            "-b:a", _AUDIO_BRATE,
            "-ar", "44100",
            "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-movflags", "+faststart",
            "-shortest",
            output_abs,
        ]

        _log(log_handler, "[Render] Concatenating + mixing audio → final MP4...")
        ok, _ = _run_ffmpeg(concat_cmd, log_handler=log_handler,
                            label="FFmpeg concat", timeout=600)
        return ok

    finally:
        # Always clean up temp files
        for seg in segment_paths:
            try:
                Path(seg).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            Path(concat_list_path).unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# MoviePy fallback — only when FFmpeg is completely unavailable
# ─────────────────────────────────────────────────────────────────────────────

def _moviepy_fallback(
    clip_paths: list[str],
    audio_path: str,
    script_text: str,
    ass_path: str | None,
    output_path: str,
    style: dict,
    log_handler=None,
) -> bool:
    _log(log_handler, "[Render] Falling back to MoviePy renderer")
    try:
        from moviepy.editor import (
            AudioFileClip, ColorClip, CompositeAudioClip, CompositeVideoClip,
            ImageClip, VideoFileClip, afx, concatenate_videoclips,
        )

        audio_abs = str(BASE_DIR / Path(audio_path))
        try:
            vc = AudioFileClip(audio_abs)
            audio_dur = vc.duration
        except Exception:
            vc = None
            audio_dur = 20.0

        num_scenes = max(len(clip_paths), 1)
        scene_dur = max(audio_dur / num_scenes, 2.0)

        seg_clips = []
        clips_open = []
        for cp in clip_paths:
            try:
                abs_cp = str(BASE_DIR / Path(cp)) if not Path(cp).is_absolute() else cp
                src = VideoFileClip(abs_cp)
                clips_open.append(src)
                c = src.subclip(0, min(scene_dur, src.duration))
                c = c.resize(height=FRAME_H)
                if c.w < FRAME_W:
                    c = c.resize(width=FRAME_W)
                c = c.crop(x_center=c.w / 2, y_center=c.h / 2, width=FRAME_W, height=FRAME_H)
                seg_clips.append(c.set_duration(scene_dur))
            except Exception:
                seg_clips.append(ColorClip(size=(FRAME_W, FRAME_H), color=style["bg"],
                                           duration=scene_dur))

        if not seg_clips:
            seg_clips = [ColorClip(size=(FRAME_W, FRAME_H), color=style["bg"], duration=5.0)]

        base = concatenate_videoclips(seg_clips, method="compose")

        audio_layers = []
        if vc:
            audio_layers.append(vc.set_start(0))
        if audio_layers:
            base = base.set_audio(CompositeAudioClip(audio_layers))

        base.write_videofile(
            output_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset="medium",
            threads=4,
            logger=None,
        )
        for c in clips_open:
            c.close()
        return True
    except Exception as exc:
        _log(log_handler, f"[Render] MoviePy fallback failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def create_reel_video(
    clip_paths: list[str],
    audio_path: str,
    script_text: str,
    ass_path: str | None = None,
    srt_path: str | None = None,    # accepted but ignored — always ASS in local mode
    log_handler=None,
    hook_text: str | None = None,
) -> str:
    """
    Render the final high-quality reel MP4.

    Returns the absolute path to the output file.

    Strategy:
    1. FFmpeg two-phase render (segments → concat + ASS burn) — primary
    2. MoviePy fallback — only if FFmpeg is not installed
    """
    ensure_storage_dirs()

    working_clips = [p for p in clip_paths[:_SCENE_LIMIT]
                     if (Path(p).is_absolute() and Path(p).exists())
                     or (BASE_DIR / Path(p)).exists()]

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_name = f"reel_{timestamp}.mp4"
    output_path = str(REELS_DIR / file_name)

    # Total duration = audio duration + 0.5s hook delay
    _DELAY_S = 0.50
    total_duration = max(len(working_clips) * 3.5, 10.0)
    probed = _get_media_duration(
        str(BASE_DIR / Path(audio_path)) if not Path(audio_path).is_absolute()
        else audio_path
    )
    if probed > 0:
        total_duration = probed + _DELAY_S + 0.3

    res_label = f"{FRAME_W}×{FRAME_H}"
    _log(log_handler, (
        f"[Render] LOCAL MODE — {res_label} / {FPS}fps / CRF{_CRF} / {_PRESET} / "
        f"{_VIDEO_BRATE} / {len(working_clips)} scenes / "
        f"{'ASS captions' if ass_path else 'no captions'}"
    ))

    if _ffmpeg_available():
        success = _ffmpeg_render(
            clip_paths=working_clips,
            audio_path=audio_path,
            output_path=output_path,
            total_duration=total_duration,
            ass_path=ass_path,
            log_handler=log_handler,
        )
        if success and Path(output_path).exists():
            size_mb = Path(output_path).stat().st_size / 1_048_576
            _log(log_handler, f"[Render] Done → {output_path} ({size_mb:.1f} MB)")
            return output_path
        _log(log_handler, "[Render] FFmpeg failed — trying MoviePy")
    else:
        _log(log_handler, "[Render] FFmpeg not found — trying MoviePy")

    ok = _moviepy_fallback(
        clip_paths=working_clips,
        audio_path=audio_path,
        script_text=script_text,
        ass_path=ass_path,
        output_path=output_path,
        style=random.choice(STYLES),
        log_handler=log_handler,
    )
    if ok:
        _log(log_handler, f"[Render] Done (MoviePy) → {output_path}")
        return output_path

    raise RuntimeError(
        "Both FFmpeg and MoviePy renderers failed. "
        "Ensure FFmpeg is installed: winget install Gyan.FFmpeg"
    )
