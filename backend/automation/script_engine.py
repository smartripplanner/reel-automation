"""
Script Engine — Gemini JSON-mode script generation with dual-track output.

Architecture:
  • ONE AI call returns a strict JSON object {display:[5 lines], voice:[5 lines]}
  • Gemini's response_mime_type="application/json" + response_schema guarantees
    the model can ONLY return JSON — no conversational filler possible.
  • Groq fallback parses the same JSON schema.
  • display[] lines  → punchy subtitles shown on screen (symbols OK)
  • voice[]   lines  → natural TTS sentences (no symbols, spelled-out numbers)
  • Smart category-aware fallback fires when both AI providers fail.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from utils.storage import SCRIPTS_DIR, ensure_storage_dirs, to_storage_relative

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Gemini JSON schema  (enforced at the API level — model CANNOT deviate)
# ─────────────────────────────────────────────────────────────────────────────
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "scenes": {
            "type": "ARRAY",
            "description": (
                "Exactly 5 scenes for the reel. Each scene has its own display text, "
                "voice text, and a unique cinematic Pexels video search query that "
                "visually matches ONLY that scene's content."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "display": {
                        "type": "STRING",
                        "description": (
                            "The ONE text for this scene — used for BOTH on-screen subtitle "
                            "AND the voiceover. Must read naturally aloud AND look punchy on screen. "
                            "No symbols. 10-14 words max. 70% English, 30% Hindi flavour phrases."
                        ),
                    },
                    "search_query": {
                        "type": "STRING",
                        "description": (
                            "2-3 word cinematic Pexels VIDEO search query for this scene. "
                            "Must visually match this scene's specific topic. "
                            "Use aesthetic concepts: 'dubai skyline night', 'laptop office dark', "
                            "'tropical beach aerial', 'cash money luxury'. "
                            "Do NOT repeat queries across scenes. Each must be unique."
                        ),
                    },
                },
                "required": ["display", "voice", "search_query"],
            },
            "minItems": 5,
            "maxItems": 5,
        },
        "format_type": {
            "type": "STRING",
            "description": (
                "Pipeline format. Use 'voiceover' for educational/financial/travel info content "
                "where a narrator speaks over B-roll. Use 'text_music' for visual/lifestyle/dance "
                "content that works better with background music and text cards."
            ),
            "enum": ["voiceover", "text_music"],
        },
        "hashtags": {
            "type": "ARRAY",
            "description": (
                "Exactly 5 highly relevant Instagram hashtags for this reel. "
                "Include the # symbol. Mix broad-reach tags (e.g. #Travel2026) "
                "with niche-specific tags (e.g. #HiddenBeachesPhilippines). "
                "All 5 must be directly relevant to the topic, location, and content."
            ),
            "items": {"type": "STRING"},
            "minItems": 5,
            "maxItems": 5,
        },
    },
    "required": ["scenes", "format_type", "hashtags"],
}

_SYSTEM_PROMPT = (
    # ── Identity ──────────────────────────────────────────────────────────────
    "You are a premium FEMALE Indian travel influencer with 10 million Instagram followers. "
    "You write SPOKEN scripts — every line will be read aloud by a TTS voice engine. "
    "Your audience: 20-35 year old urban Indians who speak Hinglish daily. "

    # ── RULE 1: Sentence structure ────────────────────────────────────────────
    # The single biggest Hinglish quality driver. Hindi naturally follows
    # Subject-Object-Verb (SOV) order. Real spoken Hinglish inherits this
    # structure — it does not simply sprinkle Hindi words into English sentences.
    "RULE 1 — SENTENCE STRUCTURE (SOV): Real Hinglish follows Hindi sentence structure. "
    "The verb or emotion comes at the END of the sentence, not the middle. "
    "WRONG (English structure): 'You should definitely visit Spiti this year.' "
    "RIGHT (SOV Hinglish):      'Spiti yaar, is saal zaroor jaao — seriously breathtaking hai!' "
    "WRONG (English structure): 'This beach is incredibly hidden and beautiful.' "
    "RIGHT (SOV Hinglish):      'Yeh beach, bilkul hidden hai — aur seriously stunning!' "

    # ── RULE 2: Language blend ────────────────────────────────────────────────
    "RULE 2 — HINGLISH BLEND (NON-NEGOTIABLE): 70% English, 30% Hindi. "
    "English carries facts, place names, numbers. Hindi adds emotion, flavour, personality. "
    "NEVER write a sentence that is 100% English OR 100% Hindi. "
    "Switch languages at most ONCE per sentence — not mid-phrase repeatedly. "
    "GOOD: 'Yeh secret waterfall in Laos is seriously magical — trust me yaar!' "
    "GOOD: 'Agar budget tight hai, then Tbilisi is your perfect answer.' "
    "BAD (too English): 'The waterfall in Laos is magical.' "
    "BAD (too many switches): 'Yeh stunning jagah hai aur yaar it is bilkul amazing woh.' "

    # ── RULE 3: No translation pairs ─────────────────────────────────────────
    # Translation pairs waste words and kill authenticity — real bilingual
    # speakers never translate themselves within the same breath.
    "RULE 3 — NO TRANSLATION PAIRS: Never say the same thing in both languages. "
    "BANNED: 'Every day, har roz' / 'Today, aaj' / 'Beautiful, sundar hai' "
    "BANNED: 'Go there, wahan jaao' / 'Save karo, save this' / 'This place, yeh jagah' "
    "If you catch yourself saying the same idea twice — in two languages — delete one. "

    # ── RULE 4: Travel terms stay English ────────────────────────────────────
    "RULE 4 — TRAVEL TERMS ALWAYS IN ENGLISH: "
    "visa, passport, hostel, hotel, airbnb, booking, itinerary, budget, backpacking, "
    "trekking, hiking, flight, airport, scuba, safari, check-in, checkout, route, "
    "layover, destination. These NEVER get translated. "

    # ── RULE 5: Natural Hindi words to use freely ─────────────────────────────
    # These are common conversational words. Use them freely — the TTS engine
    # handles them perfectly when they appear in natural Hinglish context.
    "RULE 5 — USE THESE HINDI WORDS FREELY (they sound great in TTS): "
    "yaar, bhai, yeh, woh, kya, bilkul, ekdum, sach, kasam se, lekin, aur, toh, agar, "
    "bas, sirf, bahut, zyada, accha, jagah, log, din, raat, abhi, jaldi, "
    "dekho, socho, samjho, karo, chalo, haan, nahi, mein, se, ka, ki, ke. "

    # ── RULE 6: Female voice ──────────────────────────────────────────────────
    "RULE 6 — FEMALE VOICE: You are a woman. Use feminine Hindi grammar always. "
    "USE:   'karti hoon', 'jaati hoon', 'share karti hoon', 'mujhe lagta hai'. "
    "NEVER: masculine forms like 'karta hoon', 'jaata hoon'. "

    # ── RULE 7: Output hygiene ────────────────────────────────────────────────
    "RULE 7 — OUTPUT HYGIENE: Full standard spellings only in JSON. "
    "Write 'karna' not 'krna', 'haan' not 'hn', 'yaar' not 'yr'. "
    "Exact English spellings: Punjab, Uttarakhand, Spiti, Manali, Leh, Bali, Goa, Kerala. "

    # ── RULE 8: Hashtags ──────────────────────────────────────────────────────
    "RULE 8 — HASHTAGS: Include exactly 5 Instagram hashtags. "
    "Mix broad (#Travel2026, #HiddenGems) with niche-specific (#HiddenBeachesPhilippines). "
    "All 5 must be relevant to this reel's specific topic, location, and content. "

    # ── Mandatory scene structure ─────────────────────────────────────────────
    "MANDATORY STRUCTURE — follow exactly: "
    "Scene 1 (VIRAL HOOK): Stop the scroll in 1 second. "
    "Rotate between 4 styles — pick the one that best fits the topic: "
    "  Style A (Mistake):  'Stop going to [Famous Place], go here instead!' "
    "  Style B (Secret):   'The internet is hiding this [Place] from you...' "
    "  Style C (Budget):   'How to experience [Place] like a VIP on a budget.' "
    "  Style D (Urgency):  'If you don't add [Place] to your 2026 bucket list, you are missing out.' "
    "PERMANENTLY BANNED: '[Place] jaana band karo' (repetitive, sounds robotic). "
    "BANNED openers: 'Ruk ja yaar', 'Kya aapko pata hai', 'Aaj hum', 'Dosto', 'Namaste'. "
    "Scenes 2-4: Rapid-fire real facts. Place names, prices, travel hacks. FOMO every line. "
    "Scene 5 (CTA — PURE ENGLISH ONLY MANDATORY): "
    "The final Call-To-Action MUST be 100% English — no Hindi words, no Hinglish. "
    "This is because TTS voice engines are most stable with pure English CTAs. "
    "GOOD: 'Save this video and follow me for more daily travel hacks!' "
    "GOOD: 'Follow me now — I drop hidden travel gems every single day!' "
    "BAD: 'Follow karo warna next deal miss kar doge!' (Hinglish CTA — BANNED here) "

    # ── Pre-output quality check ──────────────────────────────────────────────
    "PRE-OUTPUT CHECK — before returning JSON, verify each scene: "
    "(a) Read it aloud in your head — does it sound like a real person talking naturally? "
    "(b) Any sentence over 14 words? Split it at the natural pause. "
    "(c) Any translation pair (same idea in both languages)? Delete one. "
    "(d) Any 100%-English sentence in scenes 1-4? Add a Hindi word. "
    "(e) Scene 5 — is it 100% English? If not, fix it. "
    "You return ONLY the JSON object as specified — no markdown, no prose."
)

_USER_PROMPT_TEMPLATE = """Write a viral Hinglish Instagram Reel script for: {topic}

Return a JSON object with these keys:

"scenes" — exactly 5 objects. Each has TWO keys:
  "display": ONE text used for BOTH subtitle AND voiceover.
             10-14 words MAX. No symbols. Follows SOV sentence structure.
             70% English (facts, places, numbers) + 30% Hindi (emotion, flavour).
             Switch languages at most ONCE per sentence.
  "search_query": 2-3 word cinematic Pexels VIDEO query for this scene (all unique).

SCENE STRUCTURE — mandatory:
  Scene 1 (VIRAL HOOK): Pick ONE style that best fits this topic:
    A (Mistake): "Stop going to [Famous Place], go here instead!"
    B (Secret):  "The internet is hiding this [Place] from you..."
    C (Budget):  "How to experience [Place] like a VIP on a budget."
    D (Urgency): "If you don't add [Place] to your 2026 bucket list, you are missing out."
    BANNED: "[Place] jaana band karo" / "Ruk ja yaar" / "Aaj hum" / "Dosto" / "Namaste"

  Scenes 2-4 (FAST FACTS): Specific place names, real prices, real numbers. FOMO every line.
    Each line: SOV structure, one Hindi emotion word minimum.
    BAD: "Bali has beautiful temples and cheap food."
    GOOD: "Bali ke temples yaar — entry free hai aur seriously breathtaking lagti hain!"

  Scene 5 (CTA — 100% PURE ENGLISH, NO EXCEPTIONS):
    "Save this video and follow me for more daily travel hacks!"
    "Follow me now — I drop hidden travel gems every single day!"
    Write a fresh variation — never copy exactly — but keep it 100% English.

"format_type" — "voiceover" (for travel facts/info) or "text_music" (lifestyle/aesthetic only)

"hashtags" — EXACTLY 5 hashtags starting with #. Mix: 2 broad + 3 niche-specific.

QUALITY CHECK BEFORE YOU OUTPUT — verify each scene:
  ✓ Reads naturally when spoken aloud?
  ✓ Under 14 words?
  ✓ No translation pair (same idea in both languages)?
  ✓ Scenes 1-4 each have at least one Hindi word?
  ✓ Scene 5 is 100% English with zero Hindi words?

Example for "best hidden places in North India":
{{
  "scenes": [
    {{"display": "Internet yeh North India spots chhupa raha hai — seriously mind-blowing!",
      "search_query": "himalayas mountain aerial drone"}},
    {{"display": "Spiti Valley yaar — duniya ki sabse remote aur stunning jagah hai.",
      "search_query": "spiti valley snow mountain road"}},
    {{"display": "Chopta, Uttarakhand mein hai — mini Switzerland vibes, budget mein perfectly fit!",
      "search_query": "uttarakhand alpine meadow sunrise"}},
    {{"display": "Tirthan Valley mein riverside camping — yeh experience seriously next level hai!",
      "search_query": "riverside camping forest india"}},
    {{"display": "Save this video and follow me for more hidden travel gems every single day!",
      "search_query": "india travel adventure cinematic"}}
  ],
  "format_type": "voiceover",
  "hashtags": ["#Travel2026", "#HiddenGems", "#NorthIndiaTravel", "#BudgetHills", "#SpitivalleyIndia"]
}}

SPELLING RULES — violation breaks TTS:
- Standard English: Punjab, Uttarakhand, Spiti, Ladakh, Manali, Shimla, Kerala, Goa,
  Dehradun, Rishikesh, Varanasi, Jaipur, Udaipur, Amritsar, Himachal, Rajasthan.
- Full Hindi words: 'ekdum' not 'ekduuum', 'shayad' not 'shyad', 'karna' not 'krna'.
- No symbols, no hashtags, no ellipsis in display text.

Now write for: {topic}"""

# ─────────────────────────────────────────────────────────────────────────────
# Emergency Hinglish fallback — fires ONLY when both Gemini AND Groq are down
# English fallbacks have been removed. The pipeline NEVER produces English-only
# content. This emergency set uses the topic name directly in Hinglish framing.
# ─────────────────────────────────────────────────────────────────────────────

def _emergency_hinglish_scenes(topic: str) -> list[dict]:
    """
    Last-resort scene set when all AI providers are unavailable.
    Follows SOV structure — no translation pairs, no "[place] jaana band karo".
    """
    t = topic.strip()
    return [
        {
            "display": f"Internet yeh {t} spots chhupa raha hai — seriously jaw-dropping!",
            "voice": f"Internet yeh {t} spots chhupa raha hai — seriously jaw-dropping!",
            "search_query": "aerial cinematic landscape drone",
        },
        {
            "display": f"{t} mein yeh jagah, seriously duniya ki sabse stunning hai yaar.",
            "voice": f"{t} mein yeh jagah, seriously duniya ki sabse stunning hai yaar.",
            "search_query": "cinematic drone nature 4k",
        },
        {
            "display": "Budget mein yeh experience — bilkul next level hai, trust me!",
            "voice": "Budget mein yeh experience — bilkul next level hai, trust me!",
            "search_query": "luxury travel aesthetic cinematic",
        },
        {
            "display": "99% travelers yeh miss kar dete hain — aur baad mein pachtate hain.",
            "voice": "99% travelers yeh miss kar dete hain — aur baad mein pachtate hain.",
            "search_query": "travel tips adventure aerial",
        },
        {
            "display": "Save this video and follow me for more hidden travel gems every day!",
            "voice": "Save this video and follow me for more hidden travel gems every day!",
            "search_query": "india travel cinematic aerial",
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Logging helper
# ─────────────────────────────────────────────────────────────────────────────

def _log(log_handler, msg: str) -> None:
    if log_handler:
        log_handler(msg)
    else:
        print(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Gemini — JSON-mode call (model CANNOT return filler text)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Robust JSON cleaner — handles markdown fences, trailing commas, stray text
# ─────────────────────────────────────────────────────────────────────────────

def _clean_json_text(raw: str) -> str:
    """
    Strip common LLM formatting artifacts so json.loads() never fails on
    an otherwise-valid payload.

    Handles:
    • ```json ... ```  and  ``` ... ```  markdown code fences
    • Leading/trailing prose outside the outermost { ... }
    • Trailing commas before } or ]  (invalid in strict JSON)
    • Escaped single-quotes that some models emit
    • BOM and non-breaking spaces
    """
    text = raw.strip()

    # 1. Remove BOM / non-breaking spaces
    text = text.lstrip("\ufeff").replace("\u00a0", " ")

    # 2. Strip markdown code fences (```json, ```JSON, ```, etc.)
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    # 3. Isolate the outermost JSON object — discard any prose before/after
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    # 4. Remove trailing commas before } or ] (common LLM mistake)
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # 5. Normalise escaped single-quotes that break standard JSON parsers
    text = text.replace("\\'", "'")

    return text


def _parse_json_safe(text: str, label: str, log_handler=None) -> dict | None:
    """Clean and parse JSON, returning None (not raising) on any failure."""
    cleaned = _clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        _log(log_handler, f"{label} JSON parse error after cleaning: {exc}")
        return None


def _call_gemini_json(prompt: str, api_key: str, model: str, log_handler=None) -> dict | None:
    """
    Call Gemini via the official google-genai SDK with response_mime_type="application/json".

    The SDK enforces JSON output natively — no regex cleaning required.
    Falls back to the raw REST path only if the SDK import fails.
    """
    model_name = model.replace("models/", "")

    # ── Primary: google-genai SDK (native JSON mode, most reliable) ──
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        _log(log_handler, "google-genai SDK not installed — run: pip install google-genai")
        genai = None

    if genai is not None:
        client = genai.Client(api_key=api_key)

        for attempt in range(1, 3):
            try:
                # Build config — disable thinking for gemini-2.5 models so
                # thinking tokens don't eat into the output budget and produce
                # truncated JSON (the "Unterminated string at char 66" bug).
                cfg_kwargs: dict = {
                    "system_instruction": _SYSTEM_PROMPT,
                    "temperature": 0.85,
                    "max_output_tokens": 2048,   # well above the ~300 tokens we need
                    "top_p": 0.95,
                    "response_mime_type": "application/json",
                }
                # ThinkingConfig is only available on 2.5+ models; silence
                # the AttributeError so older model names still work.
                try:
                    cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
                        thinking_budget=0   # disable thinking → all tokens go to output
                    )
                except AttributeError:
                    pass

                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(**cfg_kwargs),
                )

                # Log finish reason so truncation is immediately visible in logs
                try:
                    finish = response.candidates[0].finish_reason
                    if str(finish) not in {"FinishReason.STOP", "STOP", "1"}:
                        _log(log_handler, f"Gemini finish_reason={finish} — may be truncated")
                except Exception:
                    pass

                text = (response.text or "").strip()
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return _parse_json_safe(text, "Gemini-SDK", log_handler)
                _log(log_handler, f"Gemini SDK attempt {attempt}: empty response")
            except Exception as exc:
                err = str(exc)
                _log(log_handler, f"Gemini SDK attempt {attempt}: {err}")
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    if attempt == 1:
                        time.sleep(2.0)
                    continue
                break
            if attempt == 1:
                time.sleep(1.5)
        return None

    # ── Fallback: raw REST (when SDK unavailable) ──
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 2048,
            "topP": 0.95,
            "response_mime_type": "application/json",
        },
    }
    for attempt in range(1, 3):
        try:
            r = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=15,
            )
            if r.ok:
                raw = r.json()
                text = (
                    raw.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                    .strip()
                )
                if text:
                    return _parse_json_safe(text, "Gemini-REST", log_handler)
            _log(log_handler, f"Gemini REST attempt {attempt}: HTTP {r.status_code}")
            if r.status_code not in {429, 503}:
                break
        except Exception as exc:
            _log(log_handler, f"Gemini REST error attempt {attempt}: {exc}")
        if attempt == 1:
            time.sleep(1.5)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Groq — JSON-mode call (system prompt + response_format)
# ─────────────────────────────────────────────────────────────────────────────

_GROQ_SYSTEM = (
    _SYSTEM_PROMPT
    + " You MUST return ONLY a valid JSON object with exactly THREE top-level keys: "
    "'scenes', 'format_type', and 'hashtags'. "
    "'scenes' is an array of 5 objects each with 'display', 'voice', and 'search_query' keys. "
    "'hashtags' is an array of EXACTLY 5 Instagram hashtags starting with #. "
    "No markdown fences. No extra text."
)


def _call_groq_json(prompt: str, api_key: str, log_handler=None) -> dict | None:
    # llama3-8b-8192 was deprecated on Groq — use llama-3.1-8b-instant instead.
    # It's the direct successor: same speed, same context window, better quality.
    body = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": _GROQ_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},  # Groq JSON mode
    }
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=12,
        )
        if r.ok:
            text = r.json()["choices"][0]["message"]["content"].strip()
            parsed = _parse_json_safe(text, "Groq", log_handler)
            if parsed:
                return parsed
        _log(log_handler, f"Groq failed: HTTP {r.status_code}")
    except Exception as exc:
        _log(log_handler, f"Groq error: {exc}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public AI fallback helper (also used by hook_engine)
# ─────────────────────────────────────────────────────────────────────────────

def generate_with_ai_fallback(
    prompt: str,
    log_handler=None,
    purpose: str = "content",
) -> tuple[str, str | None]:
    """Plain-text AI call used by hook_engine. Returns (text, provider)."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    gemini_model = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
    groq_key = os.getenv("GROQ_API_KEY")

    # Gemini plain-text call (no JSON schema for hook generation)
    if gemini_key:
        model_name = gemini_model.replace("models/", "")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_name}:generateContent?key={gemini_key}"
        )
        body = {
            "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 128},
        }
        for attempt in range(1, 3):
            try:
                r = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=10)
                if r.ok:
                    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    text = "\n".join(p.get("text", "") for p in parts).strip()
                    if text:
                        _log(log_handler, f"{purpose.capitalize()} via Gemini")
                        return text, "gemini"
                _log(log_handler, f"Gemini attempt {attempt}: HTTP {r.status_code}")
                if r.status_code not in {429, 503}:
                    break
            except Exception as exc:
                _log(log_handler, f"Gemini error: {exc}")
            if attempt == 1:
                time.sleep(1.5)

    if groq_key:
        body2 = {
            "model": "llama-3.1-8b-instant",   # replaces deprecated llama3-8b-8192
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.85,
            "max_tokens": 128,
        }
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json=body2,
                timeout=10,
            )
            if r.ok:
                text = r.json()["choices"][0]["message"]["content"].strip()
                if text:
                    _log(log_handler, f"{purpose.capitalize()} via Groq")
                    return text, "groq"
        except Exception as exc:
            _log(log_handler, f"Groq error: {exc}")

    return "", None


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

_BANNED_STARTS = (
    "here is your", "here's your", "here is the", "in this video",
    "this reel", "this video", "today we", "let's talk", "welcome to",
    "hi everyone", "hey everyone", "alright,", "okay so",
    "sure,", "certainly", "of course",
)


def _line_is_valid(line: str) -> bool:
    s = line.strip().lower()
    if not s or len(s) < 5:
        return False
    return not any(s.startswith(b) for b in _BANNED_STARTS)


def _validate_section(lines: list, topic_words: set[str]) -> bool:
    """Need 5 real lines with at least 1 mentioning a topic keyword."""
    valid = [l for l in lines if isinstance(l, str) and _line_is_valid(l)]
    if len(valid) < 4:
        return False
    return any(w in " ".join(valid).lower() for w in topic_words) if topic_words else True


# ─────────────────────────────────────────────────────────────────────────────
# Saving
# ─────────────────────────────────────────────────────────────────────────────

def _save_script(topic: str, display_lines: list[str], voice_lines: list[str]) -> str:
    ensure_storage_dirs()
    fname = f"script_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    path = SCRIPTS_DIR / fname
    display_block = "\n".join(display_lines)
    voice_block = "\n".join(voice_lines)
    path.write_text(
        f"Topic: {topic}\n\n--- DISPLAY ---\n{display_block}\n\n--- VOICE ---\n{voice_block}\n",
        encoding="utf-8",
    )
    return to_storage_relative(path)


# ─────────────────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────────────────

def generate_script(topic: str, hook: str | None = None, log_handler=None) -> dict:
    """
    Generate a dual-track script in ONE AI call.

    Returns
    -------
    dict with keys:
        text        — display script (5 lines, shown as subtitles)
        voice_text  — TTS script (5 natural sentences for edge-tts/gTTS)
        hook        — first display line
        script_path — saved file path
        provider    — "gemini" | "groq" | "fallback"
    """
    ensure_storage_dirs()

    topic_words = {
        w.lower() for w in re.split(r"\W+", topic)
        if len(w) > 3 and w.lower() not in {
            "best", "most", "that", "this", "with", "from", "your", "their",
            "have", "what", "when", "where", "about", "than", "into", "people",
        }
    }

    prompt = _USER_PROMPT_TEMPLATE.format(topic=topic)
    gemini_key = os.getenv("GEMINI_API_KEY")
    gemini_model = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
    groq_key = os.getenv("GROQ_API_KEY")

    result: dict | None = None
    provider = "fallback"

    # ── Try Gemini JSON mode ──
    if gemini_key:
        result = _call_gemini_json(prompt, gemini_key, gemini_model, log_handler)
        if result:
            provider = "gemini"

    # ── Try Groq JSON mode ──
    if result is None and groq_key:
        result = _call_groq_json(prompt, groq_key, log_handler)
        if result:
            provider = "groq"

    # ── Validate parsed result — extract scenes ──
    scenes: list[dict] = []
    format_type: str = "voiceover"

    if result and isinstance(result, dict):
        raw_scenes = result.get("scenes", [])
        if isinstance(raw_scenes, list):
            for s in raw_scenes:
                if not isinstance(s, dict):
                    continue
                d = str(s.get("display", "")).strip()
                # Single-source rule: voice is always identical to display.
                # The LLM outputs ONE text field; we propagate it to both
                # pipeline slots so downstream code needs no changes.
                q = str(s.get("search_query", "")).strip()
                if d and _line_is_valid(d):
                    scenes.append({
                        "display": d,
                        "voice": d,          # same text — Rule 3 enforced here
                        "search_query": q or "cinematic aerial travel",
                    })

        # Validate: need at least 4 good scenes with topic relevance
        display_check = [sc["display"] for sc in scenes]
        if not _validate_section(display_check, topic_words):
            scenes = []

        fmt = result.get("format_type", "").strip().lower()
        if fmt in {"voiceover", "text_music"}:
            format_type = fmt

    # ── Hinglish-only fallback: emergency scenes when all AI providers fail ──
    # English templates have been removed. If both Gemini and Groq produced no
    # usable scenes, the emergency Hinglish set fires. The pipeline NEVER serves
    # an English-only script regardless of provider availability.
    if len(scenes) < 4:
        _log(log_handler,
             "Gemini and Groq both unavailable — using emergency Hinglish scenes")
        scenes = _emergency_hinglish_scenes(topic)
        provider = "emergency"
    else:
        _log(log_handler, f"Script ready [{provider}] — {len(scenes)} scenes")

    # ── Pad to exactly 5 scenes using emergency scenes as gap-filler ──
    if len(scenes) < 5:
        emergency = _emergency_hinglish_scenes(topic)
        while len(scenes) < 5:
            scenes.append(emergency[len(scenes)])
    scenes = scenes[:5]

    display_lines = [s["display"] for s in scenes]
    voice_lines = [s["voice"] for s in scenes]

    # ── Extract hashtags (RULE 5) ─────────────────────────────────────────────
    # Pull from the LLM result first; fall back to topic-derived tags if the
    # model omitted the field or returned non-# strings.
    hashtags: list[str] = []
    if result and isinstance(result, dict):
        raw_tags = result.get("hashtags", [])
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                tag = str(tag).strip()
                if not tag.startswith("#"):
                    tag = "#" + tag
                if len(tag) > 1:
                    hashtags.append(tag)
        hashtags = hashtags[:5]

    if len(hashtags) < 5:
        hashtags = _fallback_hashtags(topic)

    script_path = _save_script(topic, display_lines, voice_lines)

    return {
        "topic": topic,
        "text": "\n".join(display_lines),
        "voice_text": "\n".join(voice_lines),
        "hook": display_lines[0],
        "script_path": script_path,
        "provider": provider,
        "format_type": format_type,
        "scenes": scenes,
        "hashtags": hashtags,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Hashtag fallback — fires when LLM omits or malforms the hashtags field
# ─────────────────────────────────────────────────────────────────────────────

def _fallback_hashtags(topic: str) -> list[str]:
    """
    Derive 5 topic-aware Instagram hashtags when the LLM didn't return any.

    Strategy: extract the two most meaningful words from the topic string,
    combine them into a CamelCase tag, then pad with evergreen SEA travel tags.
    """
    # Stopwords that make poor hashtag components
    _stop = {
        "the", "and", "for", "that", "this", "with", "from", "are", "was",
        "in", "on", "of", "to", "a", "an", "is", "it", "its", "you", "your",
        "best", "most", "hidden", "about", "how", "why", "what", "when",
    }

    words = [
        w.strip(".,!?-")
        for w in re.split(r"\s+", topic.lower())
        if len(w.strip(".,!?-")) >= 4 and w.strip(".,!?-").lower() not in _stop
    ][:3]

    custom = [f"#{w.capitalize()}" for w in words]

    evergreen = [
        "#Travel2026",
        "#HiddenGems",
        "#SoutheastAsia",
        "#BudgetTravel",
        "#TravelHacks",
        "#Wanderlust",
        "#TravelTips",
    ]

    combined: list[str] = []
    seen: set[str] = set()
    for tag in custom + evergreen:
        lower = tag.lower()
        if lower not in seen:
            seen.add(lower)
            combined.append(tag)
        if len(combined) == 5:
            break

    return combined
