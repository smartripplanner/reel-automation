from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from schemas import SettingsResponse, SettingsUpdate
from services.settings_service import get_or_create_settings, update_settings


router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsResponse)
async def get_settings_route(db: Session = Depends(get_db)):
    return get_or_create_settings(db)


@router.post("/settings", response_model=SettingsResponse)
async def update_settings_route(payload: SettingsUpdate, db: Session = Depends(get_db)):
    return update_settings(db, payload)
