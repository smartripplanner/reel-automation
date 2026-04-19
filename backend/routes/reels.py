from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import ReelResponse
from services.reel_service import get_reel, list_reels


router = APIRouter(tags=["reels"])


@router.get("/reels", response_model=list[ReelResponse])
async def list_reels_route(db: Session = Depends(get_db)):
    return list_reels(db)


@router.get("/reels/{reel_id}", response_model=ReelResponse)
async def get_reel_route(reel_id: int, db: Session = Depends(get_db)):
    reel = get_reel(db, reel_id)
    if reel is None:
        raise HTTPException(status_code=404, detail="Reel not found")
    return reel
