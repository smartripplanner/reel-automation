from datetime import date

from sqlalchemy.orm import Session

from models.reel import Reel


def create_reel(db: Session, file_path: str, caption: str, status: str) -> Reel:
    reel = Reel(file_path=file_path, caption=caption, status=status)
    db.add(reel)
    db.commit()
    db.refresh(reel)
    return reel


def list_reels(db: Session) -> list[Reel]:
    return db.query(Reel).order_by(Reel.created_at.desc()).all()


def get_reel(db: Session, reel_id: int) -> Reel | None:
    return db.query(Reel).filter(Reel.id == reel_id).first()


def count_reels_created_today(db: Session) -> int:
    reels = db.query(Reel).all()
    today = date.today()
    return sum(1 for reel in reels if reel.created_at.date() == today)
