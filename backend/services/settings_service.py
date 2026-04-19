from sqlalchemy.orm import Session

from models.settings import Settings
from schemas import SettingsUpdate


DEFAULT_SETTINGS = {
    "niche": "Motivation",
    "reel_duration": 30,
    "reels_per_day": 3,
}


def get_or_create_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if settings:
        return settings

    settings = Settings(**DEFAULT_SETTINGS)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def update_settings(db: Session, payload: SettingsUpdate) -> Settings:
    settings = get_or_create_settings(db)
    settings.niche = payload.niche
    settings.reel_duration = payload.reel_duration
    settings.reels_per_day = payload.reels_per_day
    db.commit()
    db.refresh(settings)
    return settings
