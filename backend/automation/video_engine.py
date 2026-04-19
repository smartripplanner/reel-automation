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

FRAME_W, FRAME_H = 1080, 1920
FPS = 30
HOOK_DURATION = 2.0           # seconds the hook screen is shown
SAFE_X = int(FRAME_W * 0.07)
SAFE_Y = int(FRAME_H * 0.10)
MUSIC_VOLUME = 0.08   # background music level — 8 % ensures fast voice is crystal clear
TEMP_RENDER_ROOT = BASE_DIR / "storage" / "tmp"

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

def _ffmpeg_render_scenes(
    clip_paths: list[str],
    audio_path: str,
    ass_path: str | None,
    music_path: str | None,
    output_path: str,
    total_duration: float,
    style: dict,
    log_handler=None,
) -> bool:
    """
    Two-pass scene-based FFmpeg render.

    Pass 1: Stitch scene clips sequentially (one per scene, equal duration)
            with audio mix → intermediate MP4.
    Pass 2: Burn ASS subtitles onto intermediate → final output.

    No hook screen — only dynamic .ass subtitles for text.
    """
    if not clip_paths:
        return False

    num_scenes = len(clip_paths)
    scene_duration = total_duration / num_scenes

    inputs: list[str] = []
    filter_parts: list[str] = []
    video_labels: list[str] = []

    audio_abs = os.path.abspath(str(BASE_DIR / Path(audio_path)))
    output_abs = os.path.abspath(output_path)
    ffmpeg_bin = _resolve_ffmpeg() or "ffmpeg"

    # ── Inputs 0..N-1: one video clip per scene ──
    #
    # WHY no zoompan:
    #   FFmpeg's zoompan filter is designed for STILL IMAGES (Ken Burns effect).
    #   When applied to a video stream it takes the FIRST FRAME and holds it for
    #   `d` output frames while zooming — every clip becomes a frozen photo.
    #   Removing it lets the actual video motion play through.
    for idx, cp in enumerate(clip_paths):
        abs_cp = os.path.abspath(str(BASE_DIR / Path(cp)))
        inputs += ["-i", abs_cp]
        source_duration = max(_get_media_duration(abs_cp), 0.1)
        target_dur = min(scene_duration, source_duration)
        target_dur = max(target_dur, 1.5)

        filter_parts.append(
            # 1. Scale up so the shorter dimension fills the frame
            f"[{idx}:v]"
            f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
            # 2. Centre-crop to exact portrait frame
            f"crop={FRAME_W}:{FRAME_H},"
            # 3. Fix SAR so pixels are square
            f"setsar=1,"
            # 4. Normalise frame-rate (many 4K clips are 60fps — cap to project FPS)
            f"fps={FPS},"
            # 5. Take only the first `target_dur` seconds of the clip
            f"trim=duration={target_dur:.3f},"
            # 6. Reset timestamps so concat stitches cleanly
            f"setpts=PTS-STARTPTS"
            f"[clip{idx}]"
        )
        video_labels.append(f"[clip{idx}]")

    # ── Concat all scene segments ──
    # After concat, add an explicit scale+crop to guarantee the muxed stream is
    # always 1080×1920 for Instagram's Graph API (max 1920 px on any axis).
    # The per-clip scale above handles most cases, but some 4K portrait clips
    # (e.g. 2160×3840) pass through at native resolution when the ratio is
    # an exact 2:1 match — this final node is the guaranteed enforcement point.
    concat_n = len(video_labels)
    concat_inputs = "".join(video_labels)
    filter_parts.append(
        f"{concat_inputs}concat=n={concat_n}:v=1:a=0[concat_v];"
        f"[concat_v]"
        f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
        f"crop={FRAME_W}:{FRAME_H},"
        f"setsar=1"
        f"[final_v]"
    )

    # ── Audio: voiceover + optional background music (ducked to MUSIC_VOLUME) ──
    #
    # Ducking strategy:
    #   • Voice   → volume=1.0  (full — always intelligible)
    #   • Music   → volume=MUSIC_VOLUME (0.10 = 10%)
    #   • amix duration=first means the mix ends when the voice ends
    #   • apad on voice ensures it never cuts off at the last syllable
    #   • No music path → skip amix entirely so FFmpeg doesn't error on
    #     a missing second audio stream
    voice_idx = num_scenes
    inputs += ["-i", audio_abs]
    afmt = "aformat=sample_rates=44100:channel_layouts=stereo"

    # adelay=500|500 — 500 ms silence before the voiceover starts.
    #   Gives the viewer half a second to register the hook visual before audio fires.
    #   Both L and R stereo channels delayed equally ("500|500").
    # atempo=1.25 — 25 % pitch-preserving speed-up.
    #   Applied AFTER adelay and BEFORE apad so only real speech is accelerated.
    # IMPORTANT: total_duration (set by caller) must include this delay so FFmpeg
    #   does not trim the final word off the end of the video.
    _VOICE_DELAY  = "500|500"   # ms per channel — half-second hook pause
    _VOICE_SPEED  = 1.25

    if music_path and Path(music_path).exists():
        music_idx = voice_idx + 1
        music_abs = os.path.abspath(music_path)
        inputs += ["-i", music_abs]
        _log(log_handler,
             f"Audio mix: voice@1.0×{_VOICE_SPEED}+500ms delay "
             f"+ music@{MUSIC_VOLUME:.2f} ({Path(music_path).name})")
        filter_parts.append(
            # Voice: normalise → full volume → 500 ms hook delay → 25 % speed-up → pad
            f"[{voice_idx}:a]{afmt},volume=1.0,"
            f"adelay={_VOICE_DELAY},atempo={_VOICE_SPEED},apad[voice_pad];"
            # Music: normalise → duck to 8 % → pad
            f"[{music_idx}:a]{afmt},volume={MUSIC_VOLUME:.2f},apad[music_pad];"
            # Mix: stop when voice ends (duration=first), 2 s dropout transition
            f"[voice_pad][music_pad]amix=inputs=2:duration=first:dropout_transition=2[final_a]"
        )
    else:
        if music_path and not Path(music_path).exists():
            _log(log_handler, f"Music file not found ({music_path}) — voiceover only")
        filter_parts.append(
            # Voice only: normalise → full volume → 500 ms hook delay → 25 % speed-up → pad
            f"[{voice_idx}:a]{afmt},volume=1.0,"
            f"adelay={_VOICE_DELAY},atempo={_VOICE_SPEED},apad[final_a]"
        )

    filter_complex = ";".join(filter_parts)

    TEMP_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
    job_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    stage1_path = os.path.abspath(str(TEMP_RENDER_ROOT / f"stage1_{job_tag}.mp4"))
    try:
        stage1_cmd = (
            [ffmpeg_bin, "-y"]
            + inputs
            + [
                "-filter_complex", filter_complex,
                "-map", "[final_v]",
                "-map", "[final_a]",
                "-c:v", "libx264",
                # Instagram Reels specification:
                #   • preset=fast   — ultrafast can produce Annex-B header quirks
                #     that Instagram's ingest rejects; fast is the sweet spot
                #   • profile high + level 4.0 — explicit H.264 compliance
                #   • yuv420p      — strips HDR / 10-bit pixel formats
                #   • colorspace / color_primaries / color_trc bt709
                #     — strips BT.2020 / HDR metadata tags from the container;
                #       without these, Pexels 4K clips retain "HDR" colour-space
                #       metadata even after re-encode, causing Instagram's
                #       processor to throw status=ERROR
                "-preset", "fast",
                "-profile:v", "high",
                "-level:v", "4.0",
                "-crf", "23",
                "-b:v", "8M",
                "-maxrate", "10M",
                "-bufsize", "10M",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
                "-ac", "2",
                "-r", str(FPS),
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                "-movflags", "+faststart",
                "-shortest",
                "-max_muxing_queue_size", "9999",
                "-t", str(total_duration),
                stage1_path,
            ]
        )

        _log(log_handler, "Rendering with FFmpeg (scene-based)...")
        ok, _stderr = _run_ffmpeg(
            stage1_cmd, log_handler=log_handler, label="FFmpeg stage 1", timeout=900
        )
        if not ok:
            return False

        # If no subtitles, stage 1 output IS the final output
        if not ass_path or not Path(ass_path).exists():
            shutil.copyfile(stage1_path, output_abs)
            return True

        # ── Pass 2: burn ASS subtitles (cwd workaround for Windows paths) ──
        abs_sub_path = os.path.abspath(ass_path)
        sub_dir = os.path.dirname(abs_sub_path) or None
        sub_file = os.path.basename(abs_sub_path).replace("'", r"\'")
        burn_filters = [
            f"ass=filename='{sub_file}':original_size={FRAME_W}x{FRAME_H}",
            f"subtitles='{sub_file}':original_size={FRAME_W}x{FRAME_H}",
        ]

        for attempt_idx, burn_filter in enumerate(burn_filters, start=1):
            stage2_cmd = [
                ffmpeg_bin, "-y",
                "-i", stage1_path,
                "-vf", burn_filter,
                "-c:v", "libx264",
                # Mirror stage 1 Instagram spec exactly — subtitle burn is a full
                # re-encode so every flag must be repeated.
                "-preset", "fast",
                "-profile:v", "high",
                "-level:v", "4.0",
                "-crf", "23",
                "-b:v", "8M",
                "-maxrate", "10M",
                "-bufsize", "10M",
                "-pix_fmt", "yuv420p",
                "-colorspace", "bt709",
                "-color_primaries", "bt709",
                "-color_trc", "bt709",
                # Re-encode audio explicitly (not copy) so the final file is a
                # fully self-contained AAC stream — avoids container mismatch
                # errors if Instagram re-inspects the audio track after subtitle burn.
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                output_abs,
            ]
            ok, _stderr = _run_ffmpeg(
                stage2_cmd,
                log_handler=log_handler,
                cwd=sub_dir,
                timeout=900,
                label=f"FFmpeg subtitle burn attempt {attempt_idx}",
            )
            if ok:
                return True
    finally:
        try:
            Path(stage1_path).unlink(missing_ok=True)
        except Exception:
            pass

    return False


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
    ass_path: str | None = None,
    log_handler=None,
    hook_text: str | None = None,
) -> str:
    """
    Render the final reel MP4 (scene-based, no hook screen).

    Each clip in clip_paths maps to one scene. Visual changes happen
    when the script topic changes. Only .ass dynamic subtitles are used
    for on-screen text — no static title overlaps.

    Strategy
    ────────
    1. FFmpeg direct (fast, ~30-90 s)
    2. MoviePy fallback  (slow, ~3-10 min, but always works)
    """
    ensure_storage_dirs()

    style = random.choice(STYLES)
    _log(log_handler, f"Style: {style['name']}")

    file_name = f"reel_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp4"
    output_path = str(REELS_DIR / file_name)

    # ── Total video duration — must account for all FFmpeg audio transforms ──
    # The raw MP3 duration from Whisper/probe is BEFORE FFmpeg processes it.
    # FFmpeg applies two transforms that change the final playback length:
    #   1. atempo=1.25 compresses the voice: actual_voice = raw_duration / 1.25
    #   2. adelay=500ms adds 0.5 s of silence at the start
    # If total_duration ignores these, FFmpeg trims the last word off the video.
    _ATEMPO   = 1.25
    _DELAY_S  = 0.50   # adelay 500 ms in seconds
    _BUFFER_S = 0.30   # small safety buffer so apad has room to breathe

    total_duration = max(len(clip_paths) * 3.0, 8.0)
    probed_audio_duration = _get_media_duration(str(BASE_DIR / Path(audio_path)))
    if probed_audio_duration > 0:
        total_duration = (probed_audio_duration / _ATEMPO) + _DELAY_S + _BUFFER_S

    # ── FFmpeg path (scene-based, no hook) ──
    if _ffmpeg_available():
        music_path = _pick_music()
        success = _ffmpeg_render_scenes(
            clip_paths=clip_paths,
            audio_path=audio_path,
            ass_path=ass_path,
            music_path=music_path,
            output_path=output_path,
            total_duration=total_duration,
            style=style,
            log_handler=log_handler,
        )
        if success:
            _log(log_handler, "Video rendered [FFmpeg]")
            return to_storage_relative(Path(output_path))
        _log(log_handler, "FFmpeg render failed → trying MoviePy")
    else:
        _log(log_handler, "FFmpeg not found on PATH → using MoviePy renderer")

    # ── MoviePy fallback ──
    ok = _moviepy_fallback(
        clip_paths=clip_paths,
        audio_path=audio_path,
        script_text=script_text,
        ass_path=ass_path,
        output_path=output_path,
        style=style,
        log_handler=log_handler,
    )
    if ok:
        _log(log_handler, "Video rendered [MoviePy fallback]")
        return to_storage_relative(Path(output_path))

    raise RuntimeError("Both FFmpeg and MoviePy renderers failed. Check logs.")
