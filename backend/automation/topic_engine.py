import random
import re
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# Topic string sanitization
# ─────────────────────────────────────────────────────────────────────────────

def _strip_leading_superlative(text: str) -> str:
    """
    Remove a leading "Best / Top / How to" before we prepend our own prefix.

    Without this, a niche saved as "Best Budget Countries to Visit in Europe"
    becomes "Best Best Budget Countries..." in the expanded topic string.
    """
    lower = text.lower()
    for prefix in ("best ", "top ", "how to ", "most "):
        if lower.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _remove_consecutive_duplicate_words(text: str) -> str:
    """
    Safety net: 'Best Best Budget' → 'Best Budget'.
    Handles any case-insensitive adjacent duplicate.
    """
    words = text.split()
    result: list[str] = []
    for word in words:
        if result and word.lower() == result[-1].lower():
            continue
        result.append(word)
    return " ".join(result)


TOPIC_CATEGORIES = {
    "travel": {
        "countries": [
            "Thailand",
            "Vietnam",
            "Indonesia",
            "Japan",
            "Portugal",
            "Turkey",
            "Mexico",
            "Georgia",
            "Bali",
            "Sri Lanka",
            "Dubai",
            "Italy",
            "Spain",
            "South Korea",
            "Malaysia",
        ],
        "goals": [
            "digital nomads",
            "budget travelers",
            "solo travelers",
            "remote workers",
            "first-time tourists",
            "honeymoon trips",
            "visa-free travel",
            "food lovers",
            "safe travel",
            "weekend escapes",
        ],
        "templates": [
            "Top 5 cheapest countries to travel in {year}",
            "Best countries for {goal} in {year}",
            "Travel budget in {country} vs {country}",
            "Why {country} is trending for {goal}",
            "Places in {country} that look unreal in {year}",
            "How much you need for 7 days in {country}",
            "Best months to visit {country} on a budget",
            "Hidden travel gems in {country} nobody talks about",
            "{country} or {country}: which is better for {goal}?",
            "What I wish I knew before traveling to {country}",
            "Most underrated countries for {goal}",
            "Top travel mistakes people make in {country}",
            "Best cities in {country} for first-time visitors",
            "Countries where your money stretches the furthest in {year}",
            "Best visa-friendly countries for {goal}",
        ],
    },
    "money": {
        "countries": [
            "USA",
            "Canada",
            "UAE",
            "Germany",
            "Singapore",
            "Australia",
            "India",
            "UK",
            "Switzerland",
            "Netherlands",
        ],
        "goals": [
            "saving money",
            "building wealth",
            "side hustles",
            "passive income",
            "freedom",
            "beginners",
            "young professionals",
            "students",
            "families",
            "remote workers",
        ],
        "templates": [
            "Money habits that quietly make you richer in {year}",
            "Best countries for {goal} in {year}",
            "How people in {country} are building wealth faster",
            "Salary in {country} vs {country}",
            "What $100 buys you in {country} right now",
            "The easiest money mistakes to fix this month",
            "How to save more without feeling broke in {year}",
            "Best side hustle ideas for {goal}",
            "Countries where your salary goes furthest",
            "The real cost of living in {country} vs {country}",
            "How beginners should think about money in {year}",
            "Fastest ways to stop wasting money every week",
            "Best income moves before the end of {year}",
            "What rich people do differently with their salary",
            "Money lessons people learn too late",
        ],
    },
    "salary": {
        "countries": [
            "USA",
            "Germany",
            "India",
            "Canada",
            "Australia",
            "UK",
            "UAE",
            "Singapore",
            "Ireland",
            "Netherlands",
        ],
        "roles": [
            "software engineer",
            "data analyst",
            "designer",
            "product manager",
            "marketer",
            "sales manager",
            "teacher",
            "nurse",
            "video editor",
            "developer",
        ],
        "templates": [
            "{role} salary in {country} vs {country}",
            "Best countries for high {role} salaries in {year}",
            "What a good salary looks like in {country} right now",
            "How much {role}s really earn in {country}",
            "Salary growth in {country} nobody talks about",
            "High-paying countries for {role}s in {year}",
            "Is moving to {country} worth it for your salary?",
            "Top countries where {role}s save more money",
            "Remote salary vs local salary in {country}",
            "Entry-level {role} salary in {country} vs {country}",
            "Best cities in {country} for {role} salaries",
            "Salary expectations before moving to {country}",
            "Where {role}s earn more and spend less",
            "How taxes change your salary in {country}",
            "Countries where your salary feels bigger than it looks",
        ],
    },
    "countries": {
        "countries": [
            "Japan",
            "Switzerland",
            "Thailand",
            "Norway",
            "Vietnam",
            "UAE",
            "Portugal",
            "Canada",
            "South Korea",
            "Singapore",
            "Italy",
            "New Zealand",
            "Iceland",
            "Turkey",
            "Mexico",
        ],
        "goals": [
            "living",
            "working",
            "traveling",
            "studying",
            "retiring",
            "saving money",
            "safety",
            "food lovers",
            "career growth",
            "remote work",
        ],
        "templates": [
            "Best countries for {goal} in {year}",
            "{country} vs {country}: which is better for {goal}?",
            "Top reasons people are moving to {country}",
            "What nobody tells you about living in {country}",
            "Cheapest countries for {goal} right now",
            "Safest countries for {goal} in {year}",
            "Countries with the best quality of life in {year}",
            "Is {country} overrated or worth the hype?",
            "Best country choices if you want {goal}",
            "Countries where life is easier than people expect",
            "Underrated countries for {goal}",
            "Why {country} is suddenly everywhere online",
            "The real cost of life in {country}",
            "Countries that feel expensive but are worth it",
            "Top country comparisons people search before moving",
        ],
    },
    "lifestyle": {
        "countries": [
            "Dubai",
            "Bali",
            "Los Angeles",
            "Singapore",
            "Tokyo",
            "London",
            "Bangkok",
            "Barcelona",
            "Toronto",
            "Sydney",
        ],
        "goals": [
            "glow-ups",
            "healthy routines",
            "productive mornings",
            "remote work",
            "work-life balance",
            "social life",
            "fitness",
            "confidence",
            "minimalism",
            "discipline",
        ],
        "templates": [
            "Lifestyle habits that changed everything in {year}",
            "Best cities for {goal} right now",
            "What a productive day looks like in {country}",
            "How to upgrade your lifestyle without spending more",
            "The {goal} routine more people should try",
            "Why everyone wants the {country} lifestyle",
            "Best places to live for {goal}",
            "Daily habits that make life feel easier",
            "{country} lifestyle vs {country} lifestyle",
            "Simple upgrades that make you look more put together",
            "The truth about work-life balance in {country}",
            "Morning habits that instantly improve your day",
            "Small lifestyle shifts with big results",
            "Where people move for a better lifestyle in {year}",
            "Healthy routines that actually stick",
        ],
    },
}


class TopicSource:
    STATIC_LIBRARY = "static-library"
    API_READY = "api-ready"


def _log(log_handler, message: str) -> None:
    if log_handler:
        log_handler(message)
    else:
        print(message)


def available_categories() -> list[str]:
    return sorted(TOPIC_CATEGORIES.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Niche alias map — maps user-facing UI niche names -> internal category keys.
#
# The React settings UI lets users type any niche string (e.g. "Motivation",
# "Finance", "Travel Tips"). These don't necessarily match the internal category
# keys (travel, money, salary, countries, lifestyle). Without this map,
# _normalize_category falls through to random.choice() and the niche setting
# is silently ignored.
#
# Add new aliases here whenever a user-facing niche is introduced.
# Keys: lowercase niche string (or any substring of it)
# Values: exact key in TOPIC_CATEGORIES
# ─────────────────────────────────────────────────────────────────────────────
_NICHE_ALIAS_MAP: dict[str, str] = {
    # Motivation / mindset -> lifestyle content
    "motivation":       "lifestyle",
    "mindset":          "lifestyle",
    "self improvement": "lifestyle",
    "self-improvement": "lifestyle",
    "personal growth":  "lifestyle",
    "personal development": "lifestyle",
    "productivity":     "lifestyle",
    "habits":           "lifestyle",
    "routine":          "lifestyle",
    "fitness":          "lifestyle",
    "health":           "lifestyle",
    "wellness":         "lifestyle",
    "glow up":          "lifestyle",
    "glow-up":          "lifestyle",
    "discipline":       "lifestyle",
    "success":          "lifestyle",
    "hustle":           "lifestyle",
    # Finance / investing -> money
    "finance":          "money",
    "financial":        "money",
    "investing":        "money",
    "investment":       "money",
    "wealth":           "money",
    "saving":           "money",
    "savings":          "money",
    "budget":           "money",
    "budgeting":        "money",
    "passive income":   "money",
    "side hustle":      "money",
    "income":           "salary",
    "earnings":         "salary",
    "paycheck":         "salary",
    "jobs":             "salary",
    "career":           "salary",
    "work":             "salary",
    # Travel / geography -> travel
    "travel":           "travel",
    "travel tips":      "travel",
    "backpacking":      "travel",
    "tourism":          "travel",
    "vacation":         "travel",
    "holiday":          "travel",
    "adventure":        "travel",
    "explore":          "travel",
    "nomad":            "travel",
    "digital nomad":    "travel",
    # Country comparisons
    "country":          "countries",
    "countries":        "countries",
    "moving abroad":    "countries",
    "expat":            "countries",
    "immigration":      "countries",
    "relocation":       "countries",
}


def _normalize_category(category_hint: str | None) -> str | None:
    """
    Map a user-provided niche string to an internal TOPIC_CATEGORIES key.

    Returns None when the niche is custom / unrecognised so the caller
    can use the raw niche string as a direct LLM topic instead of
    silently falling through to an unrelated template category.

    Resolution order
    ────────────────
    1. Exact match against TOPIC_CATEGORIES keys
    2. Exact alias map lookup (normalized == alias key)
    3. For SHORT niches only (≤ 2 words): fuzzy substring alias + category match
       — skipped for long/specific niches so "North India Travel" is NOT
         collapsed to the generic "travel" template library.
    4. None  →  caller treats the raw niche as a direct LLM topic.

    Why the word-count guard?
    ─────────────────────────
    A niche like "North India Travel" (3 words) is far more specific than the
    single "travel" template category — collapsing it loses all geographic
    context.  By requiring an EXACT alias for multi-word niches we ensure the
    LLM receives the full, user-intended topic ("Best North India Travel ideas
    and trends in 2026") instead of a random generic travel template.
    """
    if not category_hint:
        return "lifestyle"          # no niche set → sensible default

    normalized = category_hint.strip().lower()

    # 1. Exact match against category keys ("travel", "money", etc.)
    if normalized in TOPIC_CATEGORIES:
        return normalized

    # 2. Exact alias map lookup — catches known phrases like "side hustle",
    #    "digital nomad", "passive income" without any substring fuzziness.
    exact = _NICHE_ALIAS_MAP.get(normalized)
    if exact:
        return exact

    # 3. Fuzzy matching — ONLY for short niches (1-2 words).
    #    Multi-word / geographic niches ("North India Travel", "Rajasthan Food
    #    Tour") are specific enough to be treated as custom LLM topics.
    word_count = len(normalized.split())
    if word_count <= 2:
        # Longest alias wins (prevents "hustle" matching before "side hustle")
        for alias, category in sorted(_NICHE_ALIAS_MAP.items(), key=lambda x: -len(x[0])):
            if alias in normalized:
                return category

        # Substring match against bare category keys ("travelling" ⊃ "travel")
        for category in TOPIC_CATEGORIES:
            if category in normalized or normalized in category:
                return category

    # 4. Custom niche — signal caller to use niche string directly as LLM topic
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Batch-unique topic generation
# ─────────────────────────────────────────────────────────────────────────────

# Content-angle pool for custom / free-form niches.
# random.sample() picks N distinct angles so each reel in a batch covers a
# different facet of the same niche — eliminates the "same string every time"
# collision that occurred when generate_topic() was called independently per job.
_BATCH_ANGLE_POOL: list[str] = [
    "hidden gems and best-kept secrets",
    "budget tips and complete cost breakdown",
    "off-the-beaten-path destinations nobody talks about",
    "insider local tips and best times to visit",
    "most underrated spots and how to get there cheap",
    "aesthetic photography spots and travel hacks",
    "solo travel guide with real prices and safety tips",
]


def generate_unique_topics(category_hint: str | None, count: int = 3) -> list[str]:
    """
    Return `count` guaranteed-unique topic strings for a batch run.

    Replaces the pattern of calling generate_topic() independently for each
    reel — which allowed random.choice() to collide on the same template/topic.

    For known categories:
        Uses random.sample() on the template list so every reel in the batch
        has a structurally different topic format — impossible to duplicate.

    For custom / free-form niches (e.g. long descriptive niche strings):
        Applies random.sample() on _BATCH_ANGLE_POOL and appends each
        unique angle as a suffix to the base niche string, giving the LLM
        a distinct content entry-point for each reel.

    Parameters
    ----------
    category_hint : The user's niche string from settings (any length/format)
    count         : Number of unique topics to return (default 3 for batch)
    """
    category = _normalize_category(category_hint)
    year = datetime.utcnow().year

    # ── Custom / free-form niche ──────────────────────────────────────────────
    if category is None:
        # Strip leading "Best/Top" so we don't produce "Best Best Budget..."
        niche_clean = _strip_leading_superlative((category_hint or "general").strip())
        angles = random.sample(_BATCH_ANGLE_POOL, min(count, len(_BATCH_ANGLE_POOL)))
        return [
            _remove_consecutive_duplicate_words(f"Best {niche_clean} — {angle} in {year}")
            for angle in angles
        ]

    # ── Known category — random.sample on the template list ──────────────────
    config   = TOPIC_CATEGORIES[category]
    templates = config["templates"]
    countries = config.get("countries", ["Dubai"])
    goals     = config.get("goals", ["remote work"])
    roles     = config.get("roles", ["professional"])

    # Sample unique templates — structurally impossible to produce duplicates
    sampled = random.sample(templates, min(count, len(templates)))
    topics: list[str] = []

    for template in sampled:
        country_one = random.choice(countries)
        country_two = random.choice(
            [c for c in countries if c != country_one] or [country_one]
        )
        goal = random.choice(goals)
        role = random.choice(roles)

        if "{country}" in template:
            topic = (
                template
                .replace("{country}", country_one, 1)
                .replace("{country}", country_two, 1)
                .format(year=year, goal=goal, role=role)
            )
        else:
            topic = template.format(
                year=year, country=country_one, goal=goal, role=role
            )
        topics.append(topic)

    return topics


def generate_topic(category_hint: str | None = None, log_handler=None) -> dict:
    category = _normalize_category(category_hint)

    # ── Custom / unknown niche: skip template library, use niche as topic ──────
    # Example: "kids entertainment" -> "Best kids entertainment ideas in 2026"
    # The LLM in script_engine will write content specifically about this niche.
    if category is None:
        year = datetime.utcnow().year
        # Strip any leading "Best/Top/..." so we don't produce "Best Best Budget..."
        niche_clean = _strip_leading_superlative((category_hint or "general").strip())
        direct_topic = _remove_consecutive_duplicate_words(
            f"Best {niche_clean} ideas and trends in {year}"
        )
        _log(log_handler, f"Custom niche '{category_hint}' -> direct topic: '{direct_topic}'")
        return {
            "topic": direct_topic,
            "category": niche_clean.lower(),
            "source": TopicSource.STATIC_LIBRARY,
            "provider": TopicSource.API_READY,
        }

    if category_hint:
        _log(log_handler, f"Niche '{category_hint}' -> category '{category}'")
    config = TOPIC_CATEGORIES[category]
    template = random.choice(config["templates"])
    year = datetime.utcnow().year
    country_one = random.choice(config.get("countries", ["Dubai"]))
    country_two = random.choice([item for item in config.get("countries", ["Singapore"]) if item != country_one] or [country_one])
    goal = random.choice(config.get("goals", ["remote work"]))
    role = random.choice(config.get("roles", ["software engineer"]))

    topic = template.format(
        year=year,
        country=country_one,
        goal=goal,
        role=role,
    )
    if "{country}" in template:
        topic = template.replace("{country}", country_one, 1).replace("{country}", country_two, 1)
        topic = topic.format(year=year, goal=goal, role=role)

    _log(log_handler, f"Topic generated from {category} category")
    return {
        "topic": topic,
        "category": category,
        "source": TopicSource.STATIC_LIBRARY,
        "provider": TopicSource.API_READY,
    }
