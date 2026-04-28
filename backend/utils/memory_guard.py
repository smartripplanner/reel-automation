"""
Memory guard — psutil-based RAM monitoring for Render free tier.

Usage
─────
    from utils.memory_guard import log_ram, is_memory_critical

    log_ram("Before FFmpeg", log_handler)      # logs "[RAM] Before FFmpeg: 312 MB"
    if is_memory_critical():                   # True if RSS > 420 MB
        # degrade gracefully
        ...

Why psutil
──────────
Render free tier kills the container when it exceeds 512 MB RSS.
Monitoring RSS at each pipeline stage lets us:
  1. Log exactly where memory spikes happen (for debugging)
  2. Degrade gracefully before hitting the limit:
       - Skip subtitles (saves ~5-15 MB ASS parsing)
       - Reduce font size (no impact on memory, keeps output clean)
       - Drop to 360p fallback resolution (halves FFmpeg decode buffer)

Install
───────
psutil is in requirements.txt.  If missing, all functions return safe defaults
(0 MB, not critical) — never crashes the pipeline.
"""

from __future__ import annotations


# Threshold above which we consider memory "critical" and start degrading
_CRITICAL_THRESHOLD_MB = 420.0

# Threshold above which we drop to 360p emergency fallback
_EMERGENCY_THRESHOLD_MB = 460.0


def get_ram_mb() -> float:
    """Return current process RSS in MB, or 0.0 if psutil is unavailable."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def is_memory_critical(threshold_mb: float = _CRITICAL_THRESHOLD_MB) -> bool:
    """True if current RAM usage exceeds threshold_mb."""
    ram = get_ram_mb()
    return ram > 0 and ram > threshold_mb


def is_memory_emergency(threshold_mb: float = _EMERGENCY_THRESHOLD_MB) -> bool:
    """True if RAM is dangerously close to the 512 MB container limit."""
    ram = get_ram_mb()
    return ram > 0 and ram > threshold_mb


def log_ram(stage: str, log_handler=None) -> float:
    """
    Log current RAM usage with a stage label.

    Always logs to stdout (visible in Render logs) and optionally to the
    job's log_handler for frontend display.

    Returns current RAM in MB.
    """
    ram = get_ram_mb()
    if ram > 0:
        msg = f"[RAM] {stage}: {ram:.0f} MB"
        print(msg, flush=True)
        if log_handler:
            log_handler(msg)
    return ram
