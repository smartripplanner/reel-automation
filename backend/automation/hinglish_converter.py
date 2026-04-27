"""
Hinglish → Devanagari Converter
================================

Why this exists
---------------
Roman Hindi (writing Hindi words in English letters) is inherently ambiguous
for TTS engines. "yaar" could be read as "year", "bhai" as "bye", "mein" as
"mane". No amount of phonetic substitution fully solves this — you are asking
the engine to guess Hindi pronunciation from English letter patterns.

The permanent fix: convert Roman Hindi words to Devanagari script BEFORE
sending to ElevenLabs. eleven_multilingual_v2 reads Devanagari natively and
pronounces every word perfectly every time — no guessing.

    "yaar"  → "यार"   ← TTS knows exactly how to pronounce यार
    "bhai"  → "भाई"   ← no more "bye" misread
    "mein"  → "में"   ← no more "mane"

English travel terms (visa, hostel, budget, trekking, etc.) are explicitly
protected — they are never converted.

Applied ONLY to the TTS audio path. Subtitles always receive the original
Roman Hinglish text — display and audio are separate downstream of this call.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# English travel/technical terms — never convert these
# ─────────────────────────────────────────────────────────────────────────────

_PROTECTED_ENGLISH: set[str] = {
    # Travel logistics
    "visa", "passport", "itinerary", "hostel", "hotel", "resort", "airbnb",
    "booking", "budget", "backpacking", "backpack", "solo", "trip", "tour",
    "travel", "flight", "airport", "terminal", "checkin", "checkout",
    "transfer", "transit", "layover", "stopover", "destination", "route",
    # Activities
    "trekking", "hiking", "rafting", "scuba", "snorkeling", "paragliding",
    "bungee", "zipline", "safari", "cycling", "kayaking", "camping",
    # Accommodation
    "guesthouse", "homestay", "dormitory",
    # Food/finance
    "cafe", "restaurant", "buffet", "atm", "currency", "upi",
    # English emotion/emphasis (keep natural)
    "seriously", "literally", "vibes", "wild", "insane", "amazing",
    "jaw-dropping", "next", "level", "mind-blowing", "unreal",
}

# ─────────────────────────────────────────────────────────────────────────────
# Hinglish → Devanagari dictionary (travel-focused)
#
# Keys   : lowercase Roman Hindi as the LLM writes it
# Values : Devanagari equivalent — ElevenLabs reads this with perfect accent
#
# Rule: if the value equals the key → pass-through (English word in the map
# only to explicitly block it from the replacement loop).
# ─────────────────────────────────────────────────────────────────────────────

_DEVANAGARI_MAP: dict[str, str] = {

    # ── Multi-word phrases (must be matched before single words) ─────────────
    "kasam se":     "कसम से",    # I swear — prevent "kaz-am say"
    "sach mein":    "सच में",    # truly/really
    "karti hoon":   "करती हूँ",  # I do (f)
    "jaati hoon":   "जाती हूँ",  # I go (f)
    "share karti":  "शेयर करती",
    "ke liye":      "के लिए",    # for (purpose)
    "ke baad":      "के बाद",    # after
    "ke saath":     "के साथ",    # with
    "ke paas":      "के पास",    # near
    "ek dum":       "एकदम",      # completely (spaced variant)
    "nahi hai":     "नहीं है",
    "kya hai":      "क्या है",
    "bahut accha":  "बहुत अच्छा",
    "bilkul sahi":  "बिल्कुल सही",
    "yeh toh":      "यह तो",

    # ── Core emotion & emphasis ───────────────────────────────────────────────
    "yaar":         "यार",       # friend/buddy — prevent "year"
    "bhai":         "भाई",       # brother — prevent "bye"
    "yeh":          "यह",        # this
    "woh":          "वो",        # that — prevent "woe"
    "kya":          "क्या",      # what — prevent clipped "kee-ya"
    "bilkul":       "बिल्कुल",   # absolutely
    "ekdum":        "एकदम",      # completely
    "ekdumm":       "एकदम",      # elongated spelling
    "sach":         "सच",        # truth — prevent "satch"
    "sachchi":      "सच्ची",     # truly
    "kyun":         "क्यों",     # why
    "lekin":        "लेकिन",     # but
    "aur":          "और",        # and — prevent "or" (English)
    "toh":          "तो",        # so/then — prevent "toe"
    "agar":         "अगर",       # if — prevent "ay-gar"
    "bas":          "बस",        # just/enough
    "sirf":         "सिर्फ",     # only
    "bahut":        "बहुत",      # very — prevent "ba-hoot"
    "bohot":        "बहुत",      # variant of bahut
    "zyada":        "ज़्यादा",   # more/too much
    "kam":          "कम",        # less
    "accha":        "अच्छा",     # good — prevent "atch-a"
    "bura":         "बुरा",      # bad
    "sundar":       "सुंदर",     # beautiful
    "khoobsoorat":  "खूबसूरत",  # beautiful (poetic)

    # ── Travel-specific Hinglish words ───────────────────────────────────────
    "jagah":        "जगह",       # place — prevent "jag-ah"
    "jaghe":        "जगहें",     # places (plural)
    "cheez":        "चीज़",      # thing
    "cheezein":     "चीज़ें",    # things
    "log":          "लोग",       # people
    "din":          "दिन",       # day
    "raat":         "रात",       # night
    "subah":        "सुबह",      # morning — prevent "sue-bah"
    "shaam":        "शाम",       # evening — prevent "sham"
    "saal":         "साल",       # year
    "baad":         "बाद",       # after
    "pehle":        "पहले",      # before/first
    "abhi":         "अभी",       # right now — prevent "ab-high"
    "jaldi":        "जल्दी",     # quickly
    "door":         "दूर",       # far
    "paas":         "पास",       # near
    "andar":        "अंदर",      # inside
    "bahar":        "बाहर",      # outside
    "upar":         "ऊपर",       # above/up
    "neeche":       "नीचे",      # below/down
    "seedha":       "सीधा",      # straight
    "rasta":        "रास्ता",    # path/road
    "raasta":       "रास्ता",    # variant

    # ── Action verbs common in travel reels ──────────────────────────────────
    "dekho":        "देखो",      # look/see — prevent "deh-ko"
    "socho":        "सोचो",      # think
    "samjho":       "समझो",      # understand
    "jao":          "जाओ",       # go
    "jaao":         "जाओ",       # variant
    "aao":          "आओ",        # come
    "chalo":        "चलो",       # let's go
    "chalte":       "चलते",      # moving/going
    "ruko":         "रुको",      # stop/wait
    "sunno":        "सुनो",      # listen
    "bolo":         "बोलो",      # say
    "batao":        "बताओ",      # tell us
    "karo":         "करो",       # do it — prevent "care-oh"
    "shuru":        "शुरू",      # start — prevent "shoo-roo" clip
    "mat":          "मत",        # don't
    "haan":         "हाँ",       # yes — prevent "han"
    "nahi":         "नहीं",      # no/not — prevent "na-high"
    "nahin":        "नहीं",      # variant

    # ── Grammar connectors ────────────────────────────────────────────────────
    "mein":         "में",       # in — prevent "mane"
    "se":           "से",        # from/with
    "ko":           "को",        # to/for
    "ka":           "का",        # of (m)
    "ki":           "की",        # of (f)
    "ke":           "के",        # of (plural)
    "hai":          "है",        # is
    "hain":         "हैं",       # are (plural)
    "tha":          "था",        # was (m)
    "thi":          "थी",        # was (f)
    "ho":           "हो",        # are (you)
    "hoga":         "होगा",      # will be (m)
    "hogi":         "होगी",      # will be (f)
    "hoon":         "हूँ",       # I am

    # ── Female voice grammar ──────────────────────────────────────────────────
    "karti":        "करती",      # doing (f) — prevent "car-tee"
    "jaati":        "जाती",      # going (f)
    "milti":        "मिलती",     # gets/meets (f)
    "lagti":        "लगती",      # feels/seems (f)
    "lagta":        "लगता",      # feels/seems (m)
    "gayi":         "गई",        # went (f)
    "aayi":         "आई",        # came (f)

    # ── Infinitives & verbal nouns ────────────────────────────────────────────
    "karna":        "करना",      # to do — prevent "kar-na" clip
    "jaana":        "जाना",      # to go
    "dekhna":       "देखना",     # to see
    "rehna":        "रहना",      # to stay/live
    "milna":        "मिलना",     # to meet
    "ghumna":       "घूमना",     # to roam/travel
    "ghoomna":      "घूमना",     # variant
    "banana":       "बनाना",     # to make (not the fruit — contextual)

    # ── Amounts (travel budget context) ──────────────────────────────────────
    "rupaye":       "रुपये",     # rupees — prevent "roo-pay-ay"
    "rupaya":       "रुपया",     # rupee
    "lakh":         "लाख",       # 100,000
    "hazaar":       "हज़ार",     # thousand
    "paisa":        "पैसा",      # money/coin — prevent "pay-sa"
    "paise":        "पैसे",      # money (plural)

    # ── Nature / geography ────────────────────────────────────────────────────
    "pahaad":       "पहाड़",     # mountain — prevent "pa-head"
    "pahaadi":      "पहाड़ी",   # mountainous
    "samandar":     "समंदर",     # sea/ocean — prevent "sam-an-der"
    "nadi":         "नदी",       # river
    "jungal":       "जंगल",      # forest — prevent English "jungle" accent
    "jungle":       "जंगल",      # variant
    "duniya":       "दुनिया",    # world — prevent "doo-ni-ya" garble
    "zindagi":      "ज़िंदगी",  # life — prevent "zin-da-gee" clip

    # ── Food & culture ────────────────────────────────────────────────────────
    "khana":        "खाना",      # food — prevent "kha-na" → "kuh-na"
    "dhaba":        "ढाबा",      # roadside eatery — prevent "dha-bah"
    "pyaar":        "प्यार",     # love
    "jugaad":       "जुगाड़",   # improvised solution

    # ── Shorthand / Gen-Z spellings the LLM sometimes outputs ────────────────
    "yrr":          "यार",       # shorthand for yaar
    "pgl":          "पागल",      # shorthand for paagal (crazy)
    "ekduuum":      "एकदम",      # elongated
    "shyad":        "शायद",      # "maybe" — common typo for shayad
    "krna":         "करना",      # stripped vowel shorthand
    "hn":           "हाँ",       # "yes" collapsed
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def convert_to_devanagari(text: str) -> str:
    """
    Replace Roman Hindi words with Devanagari in TTS-bound text.

    Strategy
    --------
    1. Multi-word phrases are substituted first (longest match wins) to prevent
       partial replacements splitting a phrase ("kasam se" → "कसम से" before
       "se" → "से" would corrupt the phrase boundary).
    2. Single words are then substituted in descending length order.
    3. Protected English terms are never touched.
    4. Numbers, punctuation, and unknown words pass through unchanged.

    Only called on the voice/audio text path. Subtitles always use the
    original Roman Hinglish text to keep on-screen text readable.
    """
    if not text:
        return text

    # ── Phase 1: multi-word phrases (longest first) ───────────────────────────
    multi_phrases = sorted(
        [(k, v) for k, v in _DEVANAGARI_MAP.items() if " " in k],
        key=lambda kv: -len(kv[0]),
    )
    for roman, devanagari in multi_phrases:
        pattern = re.compile(
            r"(?<!\w)" + re.escape(roman) + r"(?!\w)", re.IGNORECASE
        )
        text = pattern.sub(devanagari, text)

    # ── Phase 2: single words (longest first, skip pass-through entries) ──────
    single_words = sorted(
        [
            (k, v)
            for k, v in _DEVANAGARI_MAP.items()
            if " " not in k and k not in _PROTECTED_ENGLISH and k != v
        ],
        key=lambda kv: -len(kv[0]),
    )
    for roman, devanagari in single_words:
        pattern = re.compile(
            r"(?<!\w)" + re.escape(roman) + r"(?!\w)", re.IGNORECASE
        )
        text = pattern.sub(devanagari, text)

    return text
