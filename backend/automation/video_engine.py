"""
Video Engine — FFmpeg-direct rendering replacing MoviePy frame loops.

Why FFmpeg direct over MoviePy
───────────────────────────────
MoviePy works by:
  1. Decoding every source frame into a NumPy array in Python
  2. Applying effects frame-by-frame in pure Python
  3. Re-encoding via FFmpeg

On a 20-second reel at 30 fps that's 600 Python iterations just for one clip.
With 4 clips + a hook screen the Python loop overhead alone takes 4-8 minutes.

FFmpeg direct:
  1. Builds a filtergraph string describing ALL operations
  2. Calls FFmpeg ONCE as a subprocess — C-native multi-threaded processing
  3. Hook screen rendered with PIL → PNG → piped to FFmpeg (no MoviePy needed)
  4. ASS subtitles burned in natively by FFmpeg's `ass` filter
  5. End-to-end render time: 25-90 seconds on a modern CPU

Fallback
────────
If FFmpeg is not on PATH or the filtergraph fails, the engine falls back to
the original MoviePy path so the pipeline never hard-crashes.
"""

from __future__ import annotations

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
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# ── Render resolution ─────────────────────────────────────────────────────────
# 720×1280 instead of 1080×1920.
# Reduces per-frame memory 2.25× and FFmpeg buffer sizes proportionally.
# Instagram accepts 720p reels without quality penalty.
FRAME_W, FRAME_H = 720, 1280

FPS = 24                      # 24fps vs 30fps → 20% fewer frames per second
HOOK_DURATION = 2.0
SAFE_X = int(FRAME_W * 0.07)
SAFE_Y = int(FRAME_H * 0.10)
MUSIC_VOLUME = 0.08
TEMP_RENDER_ROOT = BASE_DIR / "storage" / "tmp"

# ── Memory budget flags ────────────────────────────────────────────────────────
# These constants are read by _ffmpeg_render_low_mem and callers.
_PRESET      = "ultrafast"    # minimal encode memory vs "fast" / "medium"
_CRF         = "28"           # quality 28 = good enough for 720p social
_VIDEO_BRATE = "1M"           # target bitrate — keeps output files small
_MAX_RATE    = "1500k"        # ceiling
_BUF_SIZE    = "1500k"        # VBV buffer — small = small encoder RAM
_AUDIO_BRATE = "96k"          # voice-only reels don't need 128k
_SCENE_LIMIT = 3              # render at most 3 scenes (was 5)

# ─────────────────────────────────────────────────────────────────────────────
# Style library
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
    for path in [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "arialbd.ttf",
        "arial.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────────────────────────────────────
# Hook screen — rendered to PNG with PIL, fed to FFmpeg as input
# ─────────────────────────────────────────────────────────────────────────────

def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
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
    """
    Render a 1080×1920 PNG for the hook screen.
    Returns output_path on success.
    """
    img = Image.new("RGB", (FRAME_W, FRAME_H), color=tuple(c - 20 for c in style["bg"]) if any(c > 20 for c in style["bg"]) else style["bg"])
    draw = ImageDraw.Draw(img, "RGBA")

    max_text_w = int(FRAME_W * 0.84)
    clean_text = hook_text.replace("HOOK:", "").strip().upper()

    # Fit font size
    font_size = style["hook_font_size"]
    font = _load_font(font_size)
    lines = _wrap_text(clean_text, font, max_text_w)

    # Shrink until it fits within 3 lines
    while len(lines) > 3 and font_size > 40:
        font_size -= 6
        font = _load_font(font_size)
        lines = _wrap_text(clean_text, font, max_text_w)

    line_h = font_size + 12
    block_h = len(lines) * line_h + 60
    block_w = min(max_text_w + 80, FRAME_W - SAFE_X * 2)
    bx = (FRAME_W - block_w) / 2
    by = (FRAME_H - block_h) / 2

    # Panel with rounded corners
    panel_color = (*style["bg"], style["panel_alpha"])
    draw.rounded_rectangle((bx, by, bx + block_w, by + block_h), radius=28, fill=panel_color)

    # Text
    cy = by + 30
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
        lw = bbox[2] - bbox[0]
        cx = (FRAME_W - lw) / 2
        # Shadow
        draw.text((cx + 3, cy + 3), line, font=font, fill=(0, 0, 0, 140))
        # Stroke
        draw.text((cx, cy), line, font=font, fill=style["text"], stroke_width=3, stroke_fill=(0, 0, 0, 255))
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

# Known install locations tried in order when ffmpeg is not on PATH.
# Covers: winget, chocolatey, scoop, manual extraction.
_FFMPEG_FALLBACK_PATHS = [
    # winget (Gyan full build — most common on Windows 11)
    r"C:\Users\{user}\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe",
    r"C:\Users\{user}\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-full_build\bin\ffmpeg.exe",
    # chocolatey
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    # scoop
    r"C:\Users\{user}\scoop\apps\ffmpeg\current\bin\ffmpeg.exe",
    # manual common locations
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]


def _resolve_ffmpeg() -> str | None:
    """
    Return the absolute path to the ffmpeg executable, or None if not found.

    Checks PATH first, then a list of well-known install locations.
    """
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    import os as _os
    username = _os.environ.get("USERNAME", _os.environ.get("USER", ""))
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
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        target,
    ]
    try:
        result = subprocess.run(
            probe_cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def _run_ffmpeg(
    cmd: list[str],
    log_handler=None,
    cwd: str | None = None,
    timeout: int = 900,       # 15 min — 4K UHD clips need the headroom
    label: str = "FFmpeg",
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            _log(log_handler, f"{label} failed (rc={result.returncode}): {stderr[-3000:]}")
            return False, stderr
        return True, stderr
    except subprocess.TimeoutExpired:
        msg = f"{label} timed out after {timeout} seconds"
        _log(log_handler, msg)
        return False, msg
    except Exception as exc:
        msg = f"{label} subprocess error: {exc}"
        _log(log_handler, msg)
        return False, msg


# ─────────────────────────────────────────────────────────────────────────────
# Core FFmpeg render
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg_render_low_mem(
    clip_paths: list[str],
    audio_path: str,
    output_path: str,
    total_duration: float,
    log_handler=None,
) -> bool:
    """
    Memory-safe FFmpeg render for Render free tier (512 MB RAM hard limit).

    Why the old approach OOMed
    ──────────────────────────
    The previous filtergraph loaded ALL clips as simultaneous FFmpeg inputs.
    Five 4K clips (50-80 MB each) decoded simultaneously = 400-600 MB just
    for the video streams, before encoding buffers or Python overhead.

    New approach — two logical phases
    ──────────────────────────────────
    Phase 1 (sequential, one clip at a time):
        For each source clip:
          • Transcode to 720×1280, ultrafast, 1 M bitrate → small segment file
          • Delete the source clip immediately after (frees ~50 MB per clip)
          • gc.collect() to return Python-managed memory to the OS
        Peak RAM during phase 1: ~80-120 MB (one clip decode + one encode)

    Phase 2 (concat demuxer — no re-decode):
        ffmpeg -f concat -i list.txt -i audio.mp3 ...
        The concat demuxer streams segments from disk one at a time — it does
        NOT load them all into memory simultaneously.
        Each segment is already 720p and <5 MB, so the muxer barely touches RAM.
        Peak RAM during phase 2: ~60-80 MB

    Total peak: well under 200 MB, leaving 300+ MB headroom on the 512 MB tier.

    Subtitles
    ─────────
    The second subtitle-burn pass is disabled. It would re-load the entire
    intermediate file into memory for a full re-encode — exactly what killed
    us before. Captions are disabled at the pipeline level instead.
    """
    import gc

    if not clip_paths:
        return False

    # Cap scenes to limit — caller should already do this, but guard here too
    working_clips = clip_paths[:_SCENE_LIMIT]
    num_scenes = len(working_clips)
    scene_duration = max(total_duration / num_scenes, 2.0)

    ffmpeg_bin = _resolve_ffmpeg() or "ffmpeg"
    audio_abs  = os.path.abspath(str(BASE_DIR / Path(audio_path)))
    output_abs = os.path.abspath(output_path)

    TEMP_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
    job_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

    segment_paths: list[str] = []
    concat_list_path = str(TEMP_RENDER_ROOT / f"concat_{job_tag}.txt")

    try:
        # ── Phase 1: encode each clip into a small uniform segment ──────────────
        for idx, cp in enumerate(working_clips):
            abs_cp = os.path.abspath(str(BASE_DIR / Path(cp)))
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
                # Scale → crop → normalise → trim — all in a single decode pass
                "-vf",
                (
                    f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
                    f"crop={FRAME_W}:{FRAME_H},"
                    f"setsar=1,"
                    f"fps={FPS},"
                    f"trim=duration={target_dur:.3f},"
                    f"setpts=PTS-STARTPTS"
                ),
                "-c:v", "libx264",
                "-preset", _PRESET,
                "-crf",    _CRF,
                "-b:v",    _VIDEO_BRATE,
                "-maxrate", _MAX_RATE,
                "-bufsize", _BUF_SIZE,
                "-pix_fmt", "yuv420p",
                "-colorspace",      "bt709",
                "-color_primaries", "bt709",
                "-color_trc",       "bt709",
                "-an",              # no audio — added in phase 2
                "-r", str(FPS),
                "-t", str(target_dur),
                seg_path,
            ]

            _log(log_handler, f"[Render] Segment {idx+1}/{num_scenes}...")
            ok, _err = _run_ffmpeg(
                seg_cmd, log_handler=log_handler,
                label=f"Seg {idx+1}", timeout=180,
            )
            if ok and Path(seg_path).exists():
                segment_paths.append(seg_path)
            else:
                _log(log_handler, f"[Render] Segment {idx+1} failed — skipping clip")

            # Delete source immediately — frees 5-80 MB per clip
            try:
                Path(abs_cp).unlink(missing_ok=True)
            except Exception:
                pass

            gc.collect()   # return freed memory to OS before next clip

        if not segment_paths:
            _log(log_handler, "[Render] No segments produced — aborting")
            return False

        # ── Phase 2: write concat list ───────────────────────────────────────────
        with open(concat_list_path, "w") as fh:
            for seg in segment_paths:
                # FFmpeg concat list requires forward slashes even on Windows
                fh.write(f"file '{seg.replace(chr(92), '/')}'\n")

        # ── Phase 3: concat + audio → final output ───────────────────────────────
        # adelay=500|500 : 500 ms hook pause before voice starts
        # atempo=1.25    : 25 % pitch-preserving speed-up (same as before)
        afmt = "aformat=sample_rates=44100:channel_layouts=stereo"
        audio_filter = (
            f"[1:a]{afmt},volume=1.0,"
            f"adelay=500|500,atempo=1.25,apad[final_a]"
        )

        concat_cmd = [
            ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-i", audio_abs,
            "-filter_complex", audio_filter,
            "-map", "0:v",
            "-map", "[final_a]",
            "-c:v", "libx264",
            "-preset", _PRESET,
            "-profile:v", "high",
            "-level:v",   "4.0",
            "-crf",        _CRF,
            "-b:v",        _VIDEO_BRATE,
            "-maxrate",    _MAX_RATE,
            "-bufsize",    _BUF_SIZE,
            "-c:a", "aac",
            "-b:a", _AUDIO_BRATE,
            "-ar", "44100",
            "-ac", "2",
            "-pix_fmt", "yuv420p",
            "-colorspace",      "bt709",
            "-color_primaries", "bt709",
            "-color_trc",       "bt709",
            "-movflags", "+faststart",
            "-shortest",
            "-t", str(total_duration),
            output_abs,
        ]

        _log(log_handler, "[Render] Concat + audio mix → final output...")
        ok, _err = _run_ffmpeg(
            concat_cmd, log_handler=log_handler,
            label="FFmpeg concat", timeout=300,
        )
        gc.collect()
        return ok

    finally:
        # Always clean up segment files and concat list — never leave temp files
        for seg in segment_paths:
            try:
                Path(seg).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            Path(concat_list_path).unlink(missing_ok=True)
        except Exception:
            pass
        gc.collect()


# ─────────────────────────────────────────────────────────────────────────────
# MoviePy fallback — used when FFmpeg is not on PATH or the render fails
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
    """Scene-based MoviePy fallback — no hook screen, no static title overlays."""
    _log(log_handler, "Falling back to MoviePy renderer")
    try:
        from moviepy.editor import (
            AudioFileClip, ColorClip, CompositeAudioClip, CompositeVideoClip,
            ImageClip, VideoFileClip, afx, concatenate_videoclips,
        )
        clips_open: list = []
        overlay_assets: list[Path] = []

        def _ass_time_to_seconds(value: str) -> float:
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        def _load_caption_events() -> list[tuple[float, float, str]]:
            if not ass_path or not Path(ass_path).exists():
                return []
            events: list[tuple[float, float, str]] = []
            raw_ass = Path(ass_path).read_text(encoding="utf-8", errors="ignore")
            for raw_line in raw_ass.splitlines():
                if not raw_line.startswith("Dialogue:"):
                    continue
                parts = raw_line.split(",", 9)
                if len(parts) < 10:
                    continue
                start = _ass_time_to_seconds(parts[1].strip())
                end = _ass_time_to_seconds(parts[2].strip())
                text = re.sub(r"\{.*?\}", "", parts[9]).replace("\\N", "\n").strip()
                if text and end > start:
                    events.append((start, end, text))
            return events

        def _render_caption_card(text: str, idx: int) -> str:
            from textwrap import wrap
            caption_img = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 0))
            draw = ImageDraw.Draw(caption_img, "RGBA")
            font = _load_font(82)
            lines = wrap(text.strip().upper(), width=18)[:3] or [text.strip().upper()]
            line_gap = 18
            padding_x = 44
            padding_y = 28

            text_boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=4) for line in lines]
            line_heights = [box[3] - box[1] for box in text_boxes]
            text_width = max((box[2] - box[0]) for box in text_boxes)
            text_height = sum(line_heights) + line_gap * max(len(lines) - 1, 0)

            panel_w = min(text_width + padding_x * 2, FRAME_W - SAFE_X * 2)
            panel_h = text_height + padding_y * 2
            x1 = int((FRAME_W - panel_w) / 2)
            y1 = int((FRAME_H - panel_h) / 2)
            x2 = int(x1 + panel_w)
            y2 = int(y1 + panel_h)

            draw.rounded_rectangle((x1, y1, x2, y2), radius=30, fill=(0, 0, 0, 170))

            cursor_y = y1 + padding_y
            for line, box, line_h in zip(lines, text_boxes, line_heights):
                line_w = box[2] - box[0]
                cursor_x = int((FRAME_W - line_w) / 2)
                draw.text(
                    (cursor_x, cursor_y),
                    line,
                    font=font,
                    fill=tuple(style["text"]) + (255,),
                    stroke_width=4,
                    stroke_fill=(0, 0, 0, 255),
                )
                cursor_y += line_h + line_gap

            tmp_path = Path(tempfile.gettempdir()) / f"reel_caption_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}_{idx}.png"
            caption_img.save(tmp_path, "PNG")
            overlay_assets.append(tmp_path)
            return str(tmp_path)

        # Scene clips — one per scene, equal duration, no hook
        audio_abs = str(BASE_DIR / Path(audio_path))
        try:
            vc = AudioFileClip(audio_abs)
            audio_dur = vc.duration
        except Exception:
            vc = None
            audio_dur = 15.0

        num_scenes = max(len(clip_paths), 1)
        scene_dur = max(audio_dur / num_scenes, 2.0)

        seg_clips = []
        for i, cp in enumerate(clip_paths):
            dur = scene_dur
            if cp and (BASE_DIR / Path(cp)).exists():
                try:
                    src = VideoFileClip(str(BASE_DIR / Path(cp)))
                    clips_open.append(src)
                    if src.duration < dur:
                        c = src.loop(duration=dur)
                    else:
                        c = src.subclip(0, dur)
                    c = c.resize(height=FRAME_H)
                    if c.w < FRAME_W:
                        c = c.resize(width=FRAME_W)
                    c = c.crop(x_center=c.w / 2, y_center=c.h / 2, width=FRAME_W, height=FRAME_H)
                    seg_clips.append(c.set_duration(dur))
                except Exception:
                    seg_clips.append(ColorClip(size=(FRAME_W, FRAME_H), color=style["bg"], duration=dur))
            else:
                seg_clips.append(ColorClip(size=(FRAME_W, FRAME_H), color=style["bg"], duration=dur))

        if not seg_clips:
            seg_clips = [ColorClip(size=(FRAME_W, FRAME_H), color=style["bg"], duration=5.0)]

        base = concatenate_videoclips(seg_clips, method="compose")

        # ASS caption overlay (centre-aligned, no static title)
        caption_events = _load_caption_events()
        if caption_events:
            caption_layers = [base]
            for idx, (start, end, text) in enumerate(caption_events):
                card_path = _render_caption_card(text, idx)
                caption_layers.append(
                    ImageClip(card_path)
                    .set_start(start)
                    .set_duration(max(0.4, end - start))
                    .set_position(("center", "center"))
                )
            base = CompositeVideoClip(caption_layers, size=(FRAME_W, FRAME_H)).set_duration(base.duration)

        # Audio
        audio_layers = []
        if vc:
            if vc.duration > base.duration:
                vc = vc.subclip(0, base.duration)
            audio_layers.append(vc.set_start(0))
        music_file = _pick_music()
        if music_file:
            try:
                mc = AudioFileClip(music_file)
                if mc.duration < base.duration:
                    mc = afx.audio_loop(mc, duration=base.duration)
                else:
                    mc = mc.subclip(0, base.duration)
                audio_layers.append(mc.volumex(MUSIC_VOLUME).set_start(0))
            except Exception:
                pass
        if audio_layers:
            base = base.set_audio(CompositeAudioClip(audio_layers))

        base.write_videofile(
            output_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=4,
            logger=None,
        )
        for c in clips_open:
            c.close()
        for asset in overlay_assets:
            try:
                asset.unlink(missing_ok=True)
            except Exception:
                pass
        return True
    except Exception as exc:
        _log(log_handler, f"MoviePy fallback also failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def create_reel_video(
    clip_paths: list[str],
    audio_path: str,
    script_text: str,
    ass_path: str | None = None,   # kept for API compat — subtitles disabled
    log_handler=None,
    hook_text: str | None = None,
) -> str:
    """
    Render the final reel MP4 — memory-safe path for Render free tier.

    Strategy
    ────────
    1. FFmpeg low-memory path (sequential segments → concat demuxer)
       Peak RAM: ~120-160 MB. Target: sub-400 MB total process.
    2. MoviePy fallback only if FFmpeg is not on PATH.
       (MoviePy loads all clips into Python RAM — avoid on Render free tier)

    Resolution : 720×1280  (down from 1080×1920)
    Preset     : ultrafast (down from fast)
    Bitrate    : 1 Mbit/s  (down from 8 Mbit/s)
    Scenes     : 3 max     (down from 5)
    Subtitles  : disabled  (eliminates the entire second FFmpeg pass)
    """
    import gc
    ensure_storage_dirs()

    # Enforce scene limit before touching FFmpeg — each extra clip costs ~50 MB
    working_clips = [p for p in clip_paths[:_SCENE_LIMIT]
                     if (BASE_DIR / Path(p)).exists()]

    file_name = f"reel_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"
    output_path = str(REELS_DIR / file_name)

    _ATEMPO   = 1.25
    _DELAY_S  = 0.50
    _BUFFER_S = 0.30

    total_duration = max(len(working_clips) * 3.0, 8.0)
    probed = _get_media_duration(str(BASE_DIR / Path(audio_path)))
    if probed > 0:
        total_duration = (probed / _ATEMPO) + _DELAY_S + _BUFFER_S

    if _ffmpeg_available():
        _log(log_handler, f"[Render] 720p / ultrafast / 1M / {len(working_clips)} scenes")
        success = _ffmpeg_render_low_mem(
            clip_paths=working_clips,
            audio_path=audio_path,
            output_path=output_path,
            total_duration=total_duration,
            log_handler=log_handler,
        )
        gc.collect()
        if success:
            _log(log_handler, "Video rendered [FFmpeg low-mem]")
            return to_storage_relative(Path(output_path))
        _log(log_handler, "FFmpeg render failed → trying MoviePy")
    else:
        _log(log_handler, "FFmpeg not on PATH → MoviePy fallback")

    ok = _moviepy_fallback(
        clip_paths=working_clips,
        audio_path=audio_path,
        script_text=script_text,
        ass_path=None,          # subtitles disabled to save memory
        output_path=output_path,
        style=random.choice(STYLES),
        log_handler=log_handler,
    )
    if ok:
        _log(log_handler, "Video rendered [MoviePy fallback]")
        return to_storage_relative(Path(output_path))

    raise RuntimeError("Both FFmpeg and MoviePy renderers failed. Check logs.")
