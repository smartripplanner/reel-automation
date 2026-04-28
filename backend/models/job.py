from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Job(Base):
    """
    Persistent job record — survives Render restarts.

    logs_text  : newline-separated log lines written by the worker subprocess.
                 The FastAPI server reads this column to serve GET /jobs/{id}.
                 Persisted so logs are visible even after a server restart.

    topic      : the resolved topic string for display in the dashboard.
    """
    __tablename__ = "jobs"

    id:            Mapped[str]      = mapped_column(String(36), primary_key=True)
    status:        Mapped[str]      = mapped_column(String(20), nullable=False, default="queued")
    topic:         Mapped[str|None] = mapped_column(String(200), nullable=True)
    logs_text:     Mapped[str|None] = mapped_column(Text, nullable=True)   # newline-separated
    result_json:   Mapped[str|None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str|None] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
