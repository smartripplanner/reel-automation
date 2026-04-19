from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from schemas import LogResponse
from services.log_service import list_logs


router = APIRouter(tags=["logs"])


@router.get("/logs", response_model=list[LogResponse])
async def get_logs_route(db: Session = Depends(get_db)):
    return list_logs(db)
