import random
import re

from automation.script_engine import generate_with_ai_fallback


# Hooks are grouped by emotional style.
# Each template must be ≤ 12 words and punchy.
HOOK_STYLES = {
    "curiosity": [
        "HOOK: Nobody talks about this {topic} shortcut.",
        "HOOK: The {topic} trick most discover too late.",
        "HOOK: This {topic} fact will change how you think.",
        "HOOK: The real reason {topic} is harder than it looks.",
        "HOOK: Most people get {topic} completely backwards.",
    ],
    "warning": [
        "HOOK: Stop making this {topic} mistake right now.",
        "HOOK: This {topic} habit is quietly killing your results.",
        "HOOK: If you care about {topic}, avoid this first.",
        "HOOK: You are losing money because of this {topic} error.",
        "HOOK: The {topic} advice everyone follows is wrong.",
    ],
    "money": [
        "HOOK: This {topic} move saves you time and money.",
        "HOOK: Use this {topic} strategy before you waste more.",
        "HOOK: The cheapest {topic} fix that actually works.",
        "HOOK: How smart people approach {topic} differently.",
        "HOOK: Your {topic} is costing you more than you think.",
    ],
    "shock": [
        "HOOK: This {topic} truth sounds fake. It is not.",
        "HOOK: You are probably overthinking {topic} completely.",
        "HOOK: The fastest {topic} win is not what you expect.",
        "HOOK: Nobody prepared you for this part of {topic}.",
        "HOOK: This one {topic} move changes everything. Seriously.",
    ],
    "comparison": [
        "HOOK: Average people do {topic} this way. High performers don't.",
        "HOOK: Good at {topic}? Great. Here is what better looks like.",
        "HOOK: The difference between slow and fast {topic} results.",
        "HOOK: Why some people win at {topic} and most don't.",
        "HOOK: This is what successful {topic} actually looks like.",
    ],
    "urgency": [
        "HOOK: If you don't fix this {topic} habit, start now.",
        "HOOK: Your {topic} window is closing. Here is why.",
        "HOOK: The {topic} opportunity most people are sleeping on.",
        "HOOK: Do this {topic} move before it is too late.",
        "HOOK: You need to see this {topic} before next week.",
    ],
}


def _log(log_handler, message: str) -> None:
    if log_handler:
        log_handler(message)
    else:
        print(message)


def _normalize_hook(text: str) -> str:
    cleaned = re.sub(r"^HOOK:\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.strip().strip('"').strip("'")
    # Remove any trailing label artifacts
    cleaned = re.sub(r"\s*(hook|line\s*1)\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    return f"HOOK: {cleaned}" if cleaned else "HOOK: Watch this."


def _fallback_hook(topic: str, style: str) -> str:
    template = random.choice(HOOK_STYLES[style])
    # Use first 4 words of topic for conciseness
    short_topic = " ".join(topic.split()[:5]).lower()
    return template.format(topic=short_topic)


def generate_hook(topic: str, log_handler=None) -> str:
    style = random.choice(list(HOOK_STYLES.keys()))
    short_topic = " ".join(topic.split()[:5])
    prompt = (
        f"Write ONE viral Instagram reel hook for the topic: '{topic}'\n"
        f"Style: {style}\n"
        f"Rules:\n"
        f"- Return exactly ONE line. No extra text.\n"
        f"- Format: HOOK: <text>\n"
        f"- Max 12 words after 'HOOK:'\n"
        f"- Punchy, human, emotional. Not formal.\n"
        f"- Make it specific to '{short_topic}'"
    )

    hook_text, provider = generate_with_ai_fallback(prompt, log_handler=log_handler, purpose="hook")
    if hook_text:
        first_line = hook_text.splitlines()[0]
        normalized = _normalize_hook(first_line)
        _log(log_handler, f"Hook via {(provider or 'ai').capitalize()} [{style}]")
        return normalized

    fallback = _fallback_hook(topic, style)
    _log(log_handler, f"Fallback hook [{style}]")
    return fallback
