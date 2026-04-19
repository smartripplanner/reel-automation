"""
TTS Engine — tiered voice generation.

Priority chain
──────────────
1. ElevenLabs  (eleven_multilingual_v2) — primary; Hinglish-capable; requires ELEVENLABS_API_KEY
2. edge-tts    (Microsoft neural)       — free, no key, very good
3. gTTS        (Google, robotic)        — ultimate fallback, always works

Each tier is tried in order; the first successful MP3 is returned.

Required env vars
──────────────────
ELEVENLABS_API_KEY=sk_...      ← primary TTS engine (Hinglish / multilingual)
ELEVENLABS_VOICE_ID=...        ← specific voice from your ElevenLabs dashboard

Optional overrides
──────────────────
TTS_EDGE_VOICE=en-US-AndrewNeural   # edge-tts fallback voice
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime
from math import ceil
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from utils.storage import AUDIO_DIR, BASE_DIR, ensure_storage_dirs, to_storage_relative

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Symbol → words preprocessing (shared across all engines)
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_for_tts(text: str) -> str:
    """Convert display-script symbols to spoken words."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out: list[str] = []
    for line in lines:
        t = line
        t = re.sub(r"₹\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " rupees", t)
        t = re.sub(r"€\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " euros", t)
        t = re.sub(r"£\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " pounds", t)
        t = re.sub(r"\$\s*(\d[\d,]*)", lambda m: m.group(1).replace(",", "") + " dollars", t)
        t = re.sub(r"(\d+)\s*%", r"\1 percent", t)
        t = re.sub(r"\b(\d+)k\b", r"\1 thousand", t, flags=re.IGNORECASE)
        t = re.sub(r"\b(\d+)x\b", r"\1 times", t, flags=re.IGNORECASE)
        t = t.replace("+", " plus ").replace("/", " per ").replace("&", " and ")
        t = t.replace("vs", "versus").replace("→", ". ").replace("=", " equals ")
        t = re.sub(r"[#@*•]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t and t[-1] not in ".!?":
            t += "."
        if t:
            out.append(t)
    return " ".join(out)


def _log(handler, msg: str) -> None:
    if handler:
        handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Hinglish pronunciation map — ElevenLabs audio only, never touches subtitles
# ─────────────────────────────────────────────────────────────────────────────
#
# eleven_multilingual_v2 handles most Indian words correctly, but a handful of
# short Hinglish tokens get mapped to unrelated English words by the TTS decoder
# (e.g. "bhai" → sounds like "bye", "yaar" → sounds like "year").
# This map swaps those tokens for phonetic approximations BEFORE the text goes
# to ElevenLabs.  The original spelling is always preserved for subtitles.
#
# Keys   : lowercase word as it appears in the script (whole-word match only)
# Values : phonetic form that ElevenLabs eleven_multilingual_v2 renders correctly
#
# Add new entries freely — the replacement is regex word-boundary safe so
# "bhai" won't accidentally replace the "bhai" inside another word.

_PRONUNCIATION_MAP: dict[str, str] = {
    # ── Slang / shorthand corrections ────────────────────────────────────────
    # These tokens appear in Gen-Z Hinglish scripts and are mispronounced badly
    # by ElevenLabs if sent as-is.  The replacement is the full phonetic form.
    "pgl":          "paagal",        # "crazy" shorthand → full word
    "ekduuum":      "ekdam",         # elongated slang → clean spoken form
    "ekdumm":       "ekdam",         # variant spelling → standard spoken form
    "shyad":        "shayad",        # "maybe" — common typo
    "krna":         "karna",         # infinitive "to do" — shorthand strip vowel
    "hn":           "haan",          # "yes" — shorthand collapses vowels
    "destinations": "destination",   # plural breaks ElevenLabs rhythm on Hinglish
    # ── Common Hinglish words that ElevenLabs mangles ────────────────────────
    "band":         "bund",          # "closed/stop" — prevent English "band" reading
    "bahut":        "bohot",         # "very much" — ElevenLabs clips the 'u' vowel
    "bali":         "baa-lee",       # prevent "bay-lee" (Bali the island)
    "jagah":        "jug-uh",        # "place" — prevent "jag-ah" anglicisation
    "badal":        "badd-al",       # "change/cloud" — prevent "bay-dal"
    "pahani":       "pa-haa-nee",    # prevent collapse to "pani" (water)
    "games":        "gayms",         # prevent "gems" in Hinglish TTS context
    "aaagar":       "aa-gar",        # elongated "agar" — preserve vowel stretch
    "agar":         "uh-gur",        # short form — soften so TTS doesn't clip it
    # ── Core Hinglish tokens ─────────────────────────────────────────────────
    "bhai":         "bha-i",         # prevent "bye"
    "yaar":         "yaar",          # fine as-is but explicit keeps it stable
    "kya":          "kyaa",          # elongate so it doesn't sound clipped
    "woh":          "vo",            # prevent "woe"
    "mein":         "main",          # "in" → prevent "mane"
    "hain":         "hun",           # verb "are" → prevent "hane"
    "nahi":         "na-hee",        # "no/not"
    "abhi":         "ab-hee",        # "right now"
    "bahut":        "ba-hoot",       # "very" → prevent "bah-hut"
    "kyun":         "kyoon",         # "why"
    "aur":          "or",            # conjunction "and"
    "yeh":          "yeh",           # "this"
    "wala":         "waa-la",        # suffix "the one who"
    "ekdum":        "ek-dum",        # "completely"
    "ek dum":       "ek-dum",
    "bilkul":       "bil-kul",       # "absolutely"
    "zindagi":      "zin-da-gee",    # "life"
    "dhaba":        "dha-baa",       # roadside eatery
    "jugaad":       "ju-gaad",       # improvised solution
    "karo":         "kurro",         # "do it / go ahead" — prevent British "care-oh"
    "karti":        "kurtee",        # feminine present continuous — prevent "car-tee"
    "roz":          "roze",          # "every day" — prevent "rawz" / "ross"
    "hacks":        "hax",           # prevent ElevenLabs over-enunciating as "hacks" in British accent
    # ── Indian place names that get mangled ──────────────────────────────────
    "leh":          "lay",           # prevent "lee"
    "spiti":        "spee-tee",
    "punjab":       "pun-jaab",
    "ladakh":       "la-daakh",
    "manali":       "ma-naa-lee",
    "shimla":       "shim-laa",
    "mussoorie":    "mu-soo-ree",
    "rishikesh":    "ri-shi-kesh",
    "varanasi":     "va-raa-na-see",
    "jaipur":       "jai-poor",
    "udaipur":      "u-dai-poor",
    "jodhpur":      "jodh-poor",
    "amritsar":     "am-rit-sar",
    "dehradun":     "deh-ra-doon",
    "uttarakhand":  "ut-ta-ra-khand",
    "himachal":     "hi-maa-chal",
    "rajasthan":    "ra-jas-thaan",
}


def _apply_pronunciation_map(text: str) -> str:
    """
    Replace tricky Hinglish tokens with phonetic equivalents for ElevenLabs.

    Uses whole-word regex boundaries so "bhai" matches the standalone token
    but not a substring inside a longer word.  Longest keys are tried first
    to prevent partial matches (e.g. "ek dum" before "dum").
    Applied ONLY inside _elevenlabs_tts — subtitles always use the original text.
    """
    for original, phonetic in sorted(_PRONUNCIATION_MAP.items(), key=lambda x: -len(x[0])):
        text = re.sub(
            r"(?<![^\s])" + re.escape(original) + r"(?![^\s])",
            phonetic,
            text,
            flags=re.IGNORECASE,
        )
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — ElevenLabs TTS  (eleven_multilingual_v2 — primary engine)
# ─────────────────────────────────────────────────────────────────────────────
#
# eleven_multilingual_v2 natively handles Hinglish (Hindi + English mix).
# Voice ID resolution order:
#   1. ELEVENLABS_VOICE_ID   — set by user in .env (preferred)
#   2. TTS_ELEVENLABS_VOICE_ID — legacy override key
#   3. Random pick from _ELEVEN_VOICES — safe built-in fallback

# Built-in fallback voice IDs (used only when no env var is set)
_ELEVEN_VOICES = [
    "pNInz6obpgDQGcFmaJgB",  # Adam   — deep, authoritative
    "21m00Tcm4TlvDq8ikWAM",  # Rachel — clear, professional
    "AZnzlk1XvdvUeBnXmlld",  # Domi   — energetic
    "EXAVITQu4vr4xnSDxMaL",  # Bella  — warm
    "ErXwobaYiN019PkySvjV",  # Antoni — conversational
]


def _elevenlabs_tts(text: str, output_path: Path, log_handler=None) -> bool:
    """
    Generate speech via ElevenLabs eleven_multilingual_v2.

    Reads ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID from the environment.
    Falls back gracefully to a built-in voice pool if no voice ID is configured.
    Writes the audio directly to output_path as an MP3.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        _log(log_handler, "ElevenLabs skipped — ELEVENLABS_API_KEY not set")
        return False

    # Honour both env var names: ELEVENLABS_VOICE_ID (new) and
    # TTS_ELEVENLABS_VOICE_ID (legacy), then fall back to random built-in.
    voice_id = (
        os.getenv("ELEVENLABS_VOICE_ID")
        or os.getenv("TTS_ELEVENLABS_VOICE_ID")
        or random.choice(_ELEVEN_VOICES)
    )

    try:
        from elevenlabs import ElevenLabs, VoiceSettings
    except ImportError:
        _log(log_handler, "elevenlabs package not installed — run: pip install elevenlabs")
        return False

    try:
        client = ElevenLabs(api_key=api_key)

        # Apply pronunciation map: swap tricky Hinglish tokens for phonetic
        # equivalents before sending to ElevenLabs.  Subtitles are generated
        # from the original text upstream — this change is audio-only.
        tts_text = _apply_pronunciation_map(text)

        # ── Stability log: show exactly what ElevenLabs will receive ──────────
        _log(log_handler,
             f"[TTS] Normalized script ({len(tts_text.split())} words): {tts_text[:120]}"
             f"{'…' if len(tts_text) > 120 else ''}")

        # SDK v1.x API: text_to_speech.convert() returns a byte-chunk generator.
        # (The old client.generate() method was removed in v1.0.)
        #
        # VoiceSettings tuning for Hinglish reels:
        #   stability=0.3     — lower stability = more dynamic, expressive delivery.
        #                       Higher values sound flat/monotone. 0.3 gives the
        #                       punchy, energetic cadence Indian reels need.
        #   similarity_boost=0.75 — keeps the chosen voice's character while still
        #                           allowing the expressiveness from low stability.
        #   style=0.5         — moderate style exaggeration, natural on Hinglish.
        #   use_speaker_boost — crisper output, slightly louder relative to noise.
        audio_stream = client.text_to_speech.convert(
            text=tts_text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128",   # 128 kbps MP3, 44.1 kHz stereo
            voice_settings=VoiceSettings(
                stability=0.3,
                similarity_boost=0.75,
                style=0.5,
                use_speaker_boost=True,
            ),
        )

        # Write chunks directly to disk — no intermediate memory buffer
        with open(output_path, "wb") as fh:
            for chunk in audio_stream:
                if chunk:
                    fh.write(chunk)

        ok = output_path.exists() and output_path.stat().st_size > 1024
        if ok:
            _log(log_handler,
                 f"Voice [ElevenLabs eleven_multilingual_v2 / voice:{voice_id[:12]}...] OK")
        else:
            _log(log_handler, "ElevenLabs wrote an empty/tiny file — will try next tier")
            output_path.unlink(missing_ok=True)
        return ok

    except Exception as exc:
        _log(log_handler, f"ElevenLabs TTS error: {exc}")
        output_path.unlink(missing_ok=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tier 3 — edge-tts (Microsoft neural, free)
# ─────────────────────────────────────────────────────────────────────────────

_EDGE_VOICES = [
    "en-US-AndrewNeural",
    "en-US-RyanMultilingualNeural",
    "en-US-GuyNeural",
    "en-US-AriaNeural",
    "en-US-JennyNeural",
    "en-US-SaraNeural",
]


async def _edge_tts_async(text: str, output_path: Path, voice: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def _edge_tts(text: str, output_path: Path, log_handler=None) -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        _log(log_handler, "edge-tts not installed — run: pip install edge-tts")
        return False
    voice = os.getenv("TTS_EDGE_VOICE", random.choice(_EDGE_VOICES))
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            fut = asyncio.run_coroutine_threadsafe(
                _edge_tts_async(text, output_path, voice), loop
            )
            fut.result(timeout=30)
        else:
            asyncio.run(_edge_tts_async(text, output_path, voice))

        ok = output_path.exists() and output_path.stat().st_size > 0
        if ok:
            _log(log_handler, f"Voice [edge-tts / {voice}]")
        return ok
    except Exception as exc:
        _log(log_handler, f"edge-tts error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tier 4 — gTTS (robotic but always works)
# ─────────────────────────────────────────────────────────────────────────────

def _gtts(text: str, output_path: Path, log_handler=None) -> bool:
    try:
        from gtts import gTTS
        gTTS(text=text, lang="en", slow=False).save(str(output_path))
        ok = output_path.exists() and output_path.stat().st_size > 0
        if ok:
            _log(log_handler, "Voice [gTTS fallback]")
        return ok
    except Exception as exc:
        _log(log_handler, f"gTTS error: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Silent stub (pipeline never hard-crashes)
# ─────────────────────────────────────────────────────────────────────────────

def _silent_stub(output_path: Path, word_count: int) -> None:
    from moviepy.audio.AudioClip import AudioArrayClip
    fps = 44100
    dur = max(ceil(word_count / 2.8), 8)
    stereo = np.zeros((max(int(dur * fps), 1), 2), dtype=np.float32)
    clip = AudioArrayClip(stereo, fps=fps)
    try:
        clip.write_audiofile(str(output_path), fps=fps, codec="mp3", logger=None)
    finally:
        clip.close()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_voice(
    script_text: str,
    log_handler=None,
    voice_text: str | None = None,
) -> str:
    """
    Generate an MP3 voice track using the best available TTS engine.

    Tier chain (tried in order until one succeeds):
        1. ElevenLabs eleven_multilingual_v2  ← primary (Hinglish-capable)
        2. edge-tts Microsoft Neural          ← free fallback
        3. gTTS Google                        ← last resort

    Parameters
    ----------
    script_text : str — display script (symbols OK, used when voice_text absent)
    voice_text  : str — natural-language TTS version (preferred; no symbols)

    Returns storage-relative path to the generated MP3.
    """
    ensure_storage_dirs()
    fname = f"voice_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.mp3"
    out = AUDIO_DIR / fname

    raw = (voice_text or script_text or "").strip()
    if not raw:
        raw = "Watch this and follow for more."

    tts_text = _prepare_for_tts(raw)

    for tier_fn in [_elevenlabs_tts, _edge_tts, _gtts]:
        if tier_fn(tts_text, out, log_handler):
            return to_storage_relative(out)

    # Last resort — silent audio so video render doesn't crash
    _log(log_handler, "All TTS engines failed — writing silent audio stub")
    _silent_stub(out, len(tts_text.split()))
    return to_storage_relative(out)


def voice_file_exists(relative_path: str) -> bool:
    return (BASE_DIR / relative_path).exists()
