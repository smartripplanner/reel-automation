from sqlalchemy.orm import Session

from models.log import Log


def create_log(db: Session, message: str) -> Log:
    log_entry = Log(message=message)
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return log_entry


def list_logs(db: Session, limit: int = 50) -> list[Log]:
    return db.query(Log).order_by(Log.timestamp.desc()).limit(limit).all()
