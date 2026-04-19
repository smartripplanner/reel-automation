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
    "You are a premium FEMALE Indian travel influencer with 10 million Instagram followers. "
    "You MUST write in a conversational 'Hinglish' blend (70% English, 30% Roman Hindi). "
    "RULE 1 — HINGLISH LANGUAGE BLEND (NON-NEGOTIABLE): "
    "At least 3 sentences in every script MUST contain conversational Hindi words "
    "(e.g., 'Yaar', 'Kasam se', 'Ekdum', 'Sach mein', 'Dekho', 'Bilkul', 'Yeh'). "
    "You MUST mix Hindi and English words in the SAME sentence — "
    "never write a sentence that is 100% English OR 100% Hindi. "
    "GOOD: 'Yeh secret waterfalls in Laos are seriously magical, trust me yaar!' "
    "GOOD: 'Agar budget kam hai, then this place is absolutely perfect for you!' "
    "BAD (too English — PERMANENTLY BANNED): 'The waterfalls in Laos are magical.' "
    "Never sound like a formal travel documentary. Talk like a Gen-Z female Indian influencer. "
    "English carries the facts and locations; Hindi adds the emotion, flavour, and personality. "
    "NEVER write full Hindi-only sentences. The blend must always lean 70% English. "
    "RULE 2 — SINGLE TEXT FIELD: Output exactly ONE text value per scene. "
    "DO NOT output separate subtitle and voiceover text — the same text is used for both. "
    "It must read naturally when spoken aloud AND displayed on screen simultaneously. "
    "10-14 words per scene maximum. Tight, punchy, no filler. "
    "RULE 3 — FEMALE VOICE: You are a woman. Use feminine grammar where Hindi appears "
    "('karti hoon', 'jaati hoon', 'share karti hoon'). Never use masculine forms. "
    "RULE 4 — OUTPUT HYGIENE: Full words only in JSON. "
    "Write 'karna' not 'krna', 'haan' not 'hn'. "
    "Exact standard spellings: Punjab, Uttarakhand, Spiti, Manali, Leh, Bali, Goa, Kerala. "
    "RULE 5 — HASHTAGS (NON-NEGOTIABLE): You MUST include a 'hashtags' key in your JSON "
    "containing EXACTLY 5 highly relevant Instagram hashtags. "
    "Each hashtag MUST start with the # symbol. "
    "Mix broad-reach tags (e.g. #Travel2026, #HiddenGems) with niche-specific tags "
    "(e.g. #HiddenBeachesPhilippines, #BudgetTravelSEA). "
    "All 5 must be directly relevant to the topic, location, and content of the reel. "
    "MANDATORY STRUCTURE: "
    "Scene 1 (VIRAL HOOK — NON-NEGOTIABLE): Must stop the scroll in 1 second. "
    "CRITICAL RULE: You MUST randomly rotate between these 4 hook styles based on the topic. "
    "NEVER use the exact same phrasing twice. Adapt the hook dynamically to the specific location. "
    "Style A (The Mistake): Tell viewers they are making a mistake with a popular choice. "
    "  e.g., 'Stop going to [Famous Place], go here instead!' "
    "Style B (The Secret): Reveal something the internet is hiding from them. "
    "  e.g., 'The internet is hiding this beautiful [Place] from you...' "
    "Style C (The Budget): Promise a VIP-level experience on a budget. "
    "  e.g., 'How to travel to [Place] like a VIP on a budget.' "
    "Style D (The Urgency): Create FOMO around a time-sensitive bucket-list moment. "
    "  e.g., 'If you don't add this to your 2026 bucket list, you are missing out.' "
    "PERMANENTLY BANNED pattern — causes repetition, never use: '[Place] jaana band karo'. "
    "BANNED openers (kills retention instantly — NEVER use): "
    "'Ruk ja yaar', 'Kya aapko pata hai', 'Aaj hum', 'Dosto', 'Namaste', 'Hi everyone'. "
    "Scenes 2-4: Rapid-fire real facts — place names, prices, experiences. FOMO every line. "
    "Scene 5 (CTA — PURE ENGLISH ONLY): CRITICAL: The final Call-To-Action MUST be in pure, "
    "flawless English so the voice engine does not glitch. "
    "e.g., 'Save this video and follow me for more daily travel hacks!' "
    "Do NOT use Hinglish for the final scene. Pure English only. "
    "You return ONLY the JSON object as specified — no markdown, no prose."
)

_USER_PROMPT_TEMPLATE = """Write a viral Hinglish Instagram Reel script for this exact topic: {topic}

Return a JSON object with two keys:

"scenes" — array of exactly 5 objects. Each object has TWO keys only:
  "display": ONE text for this scene used for BOTH subtitle AND voiceover.
             70% English + 30% Hindi flavour words. 10-14 words MAX.
             No symbols. Must sound natural spoken aloud AND look punchy on screen.
             Example: "This hidden beach in Goa is unreal — seriously yaar, mind-blowing!"
  "search_query": a 2-3 word cinematic Pexels VIDEO search query that visually
                  matches THIS specific scene. Every scene MUST have a DIFFERENT
                  query. Use aesthetic, visual concepts — not actions.

STRICT scene structure — follow this exactly:
  Scene 1 (VIRAL HOOK — NON-NEGOTIABLE): Must stop the scroll in 1 second.
                  CRITICAL: Randomly rotate between these 4 styles. Pick one that best fits
                  the topic and NEVER repeat the same phrasing across different reels.
                  - Style A (The Mistake):  e.g., "Stop going to [Famous Place], go here instead!"
                  - Style B (The Secret):   e.g., "The internet is hiding this beautiful [Place] from you..."
                  - Style C (The Budget):   e.g., "How to travel to [Place] like a VIP on a budget."
                  - Style D (The Urgency):  e.g., "If you don't add this to your 2026 bucket list, you are missing out."
                  PERMANENTLY BANNED (causes repetition — never use): "[Place] jaana band karo"
                  BANNED openers (NEVER use): "Ruk ja yaar", "Kya aapko pata hai",
                  "Kya tum jaante ho", "Aaj hum", "Dosto", "Namaste", "Main aaj".
  Scenes 2-4 (FAST FACTS): Rapid-fire, specific. Real place names, real prices, real numbers.
                  Each fact must create FOMO — viewer must feel they are missing out RIGHT NOW.
  Scene 5 (CTA — PURE ENGLISH ONLY — MANDATORY):
                  CRITICAL: The final Call-To-Action MUST be in pure, flawless English
                  so the voice engine does not glitch. Do NOT use Hinglish here.
                  Approved examples (use one or write a fresh variation):
                  "Save this video and follow me for more daily travel hacks!"
                  "Follow me now — I drop hidden travel gems every single day!"
                  "Don't miss out — save this and follow for more travel secrets!"

"format_type" — "voiceover" or "text_music"
  "voiceover" for travel, food, finance, facts, or any informational content
  "text_music" for pure lifestyle, dance, or aesthetic-only content

"hashtags" — array of EXACTLY 5 Instagram hashtags for this topic.
  MANDATORY: every entry must start with #.
  Mix reach levels: 2 broad tags + 3 niche-specific tags.
  Example: ["#Travel2026", "#HiddenGems", "#NorthIndiaTravel", "#BudgetHills", "#SpitivalleyIndia"]

Example for topic "best hidden places in North India":
{{
  "scenes": [
    {{"display": "Internet yeh North India spots chhupa raha hai — seriously yaar, mind-blowing!",
      "voice": "Internet yeh North India ke 3 most stunning hidden spots chhupa raha hai aur main aaj yeh sab share karti hoon — genuinely jaw-dropping hai yaar!",
      "search_query": "himalayas mountain landscape"}},
    {{"display": "Spiti Valley — seriously duniya ki sabse wild jagah.",
      "voice": "Spiti Valley bhai, seriously duniya ki sabse remote aur beautiful jagah hai, aur almost koi jaanta nahi iske baare mein.",
      "search_query": "spiti valley snow mountain"}},
    {{"display": "Chopta — Uttarakhand ka mini Switzerland. Ek dum free!",
      "voice": "Chopta, Uttarakhand mein hai, aur yeh jagah seriously mini Switzerland jaisi lagti hai, aur budget mein bhi perfectly fit ho jaati hai.",
      "search_query": "uttarakhand alpine meadow"}},
    {{"display": "Tirthan Valley mein camping? Next level experience hai yaar!",
      "voice": "Tirthan Valley mein riverside camping karna ek aisa experience hai jo tumhari zindagi badal dega, seriously next level hai yaar.",
      "search_query": "riverside camping forest india"}},
    {{"display": "Follow karo warna next deal miss kar doge!",
      "voice": "Yaar save karo is reel ko aur follow karo abhi — aise hidden travel hacks roz share karta hoon, miss mat karna!",
      "search_query": "india travel adventure"}}
  ],
  "format_type": "voiceover",
  "hashtags": ["#Travel2026", "#HiddenGems", "#NorthIndiaTravel", "#BudgetHills", "#SpitivalleyIndia"]
}}

SPELLING RULES (mandatory — violation breaks the TTS engine):
- Always use EXACT standard English spellings for Indian cities and common words:
  Punjab, Uttarakhand, Spiti, Ladakh, Manali, Shimla, Rajasthan, Kerala, Goa,
  Dehradun, Rishikesh, Varanasi, Jaipur, Udaipur, Amritsar, Himachal.
- Do NOT use phonetic guesses, creative spellings, or regional variants.
- Do NOT use phonetic Hinglish slang in the JSON output (e.g. write 'paagal' not 'pgl',
  'ekdum' not 'ekduuum', 'shayad' not 'shyad').
- The 'voice' field must be clean, speakable text — no symbols, no hashtags, no ellipsis.

Now write for: {topic}"""

# ─────────────────────────────────────────────────────────────────────────────
# Emergency Hinglish fallback — fires ONLY when both Gemini AND Groq are down
# English fallbacks have been removed. The pipeline NEVER produces English-only
# content. This emergency set uses the topic name directly in Hinglish framing.
# ─────────────────────────────────────────────────────────────────────────────

def _emergency_hinglish_scenes(topic: str) -> list[dict]:
    """
    Last-resort scene set when all AI providers are unavailable.
    Always Hinglish — never English-only. Embeds the topic directly.
    """
    t = topic.strip()
    return [
        {
            "display": f"{t} jana band karo — pehle yeh dekho!",
            "voice": (
                f"Bhai {t} ke baare mein jo tum soch rahe ho woh bilkul galat hai, "
                f"pehle yeh sun lo seriously."
            ),
            "search_query": "aerial cinematic landscape drone",
        },
        {
            "display": f"{t} ka sabse bada secret koi nahi batata.",
            "voice": (
                f"{t} mein ek aisi cheez hai jo 99 percent log miss kar dete hain "
                f"aur baad mein pachtate hain yaar."
            ),
            "search_query": "cinematic drone nature 4k",
        },
        {
            "display": "Yeh hack sirf smart log jaante hain.",
            "voice": (
                "Jo log yeh jaante hain unka experience aur budget dono bilkul "
                "next level ho jaata hai, seriously wild hai bhai."
            ),
            "search_query": "luxury travel aesthetic cinematic",
        },
        {
            "display": "99% log yeh galti karte hain. Tu mat kar.",
            "voice": (
                "Bhai yeh wali common galti almost sabhi karte hain lekin isko "
                "fix karna ek dum aasaan hai, bas thoda dhyan chahiye."
            ),
            "search_query": "travel tips adventure aerial",
        },
        {
            "display": "Follow karo warna next deal miss kar doge!",
            "voice": (
                "Yaar save karo is reel ko aur follow karo abhi — aise crazy "
                "hacks roz share karta hoon, miss mat karna."
            ),
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
