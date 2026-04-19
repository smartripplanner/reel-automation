"""
Logger utilities
=================
Two logging helpers are provided:

log_message(db, message)
    Original single-threaded helper.  Reuses an existing SQLAlchemy session.
    Safe when called from a single thread only — do NOT pass into a function
    that fans out to multiple threads (e.g. the pipeline's Stage 2 audio +
    media parallel fetch), as shared-session commits are not thread-safe in
    SQLAlchemy 2.0.

log_message_safe(message)
    Thread-safe version.  Opens its own short-lived session per call, writes,
    and closes the session before returning.  A module-level threading.Lock
    serialises concurrent SQLite writes so no two threads share session state.
    Use this as the log_handler callback inside run_pipeline() — it is safe to
    call from any thread at any time without risk of session corruption.
"""

import threading
from sqlalchemy.orm import Session

from services.log_service import create_log

# ─────────────────────────────────────────────────────────────────────────────
# Module-level write lock — serialises all safe-log DB commits.
# Each call still gets its own session (eliminating shared-state corruption),
# while the lock prevents concurrent SQLite writes that could cause BUSY errors.
# ─────────────────────────────────────────────────────────────────────────────
_db_write_lock = threading.Lock()


def log_message(db: Session, message: str) -> None:
    """
    Single-threaded log writer — reuses an existing session.
    Safe only when called from ONE thread at a time.
    """
    print(f"[Reel Automation] {message}")
    create_log(db, message)


def log_message_safe(message: str) -> None:
    """
    Thread-safe log writer for use inside the pipeline's parallel stages.

    Opens a fresh SQLAlchemy session for every call so no shared session
    state is ever corrupted by concurrent commits.  The module-level lock
    guarantees that only one DB write is in flight at any given moment,
    avoiding SQLite BUSY / journal-mode conflicts.

    Failures are silently swallowed — a log-write error must never crash
    the pipeline or a background job thread.
    """
    from database import SessionLocal  # local import avoids circular dependency at module load

    print(f"[Reel Automation] {message}")
    with _db_write_lock:
        db = SessionLocal()
        try:
            create_log(db, message)
        except Exception:
            pass   # log write failure is non-fatal
        finally:
            db.close()
