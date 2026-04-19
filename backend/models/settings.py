from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    niche: Mapped[str] = mapped_column(String, default="Motivation", nullable=False)
    reel_duration: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    reels_per_day: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
