from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "storage" / "reel_automation.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_db() -> None:
    """
    Add columns that didn't exist in earlier schema versions (idempotent).

    SQLAlchemy's create_all() creates missing TABLES but does NOT add missing
    COLUMNS to an already-existing table.  This function fills that gap with
    explicit ALTER TABLE statements.  Each statement is wrapped in its own
    try/except so a "duplicate column" error from a re-run is silently ignored.
    """
    with engine.connect() as conn:
        for col_def in (
            "logs_text TEXT",
            "topic VARCHAR(200)",
        ):
            try:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # WAL (Write-Ahead Logging) allows the FastAPI server process and the
    # pipeline worker subprocess to access SQLite simultaneously:
    #   - One writer (worker) + multiple readers (server) at the same time
    #   - Prevents "database is locked" errors under concurrent access
    # NORMAL sync: safe on Render SSD, faster than FULL without data-loss risk.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    _migrate_db()
