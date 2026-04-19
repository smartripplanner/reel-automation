from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_path: str
    caption: str
    status: str
    created_at: datetime


class LogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message: str
    timestamp: datetime


class SettingsUpdate(BaseModel):
    niche: str = Field(..., min_length=1, max_length=500)
    reel_duration: int = Field(..., ge=5, le=300)
    reels_per_day: int = Field(..., ge=1, le=50)


class SettingsResponse(SettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    id: int


class AutomationStatusResponse(BaseModel):
    is_running: bool
    mode: str
    last_run_at: datetime | None
    active_job: str | None


class MessageResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    status: str


class GenerateReelResponse(BaseModel):
    message: str
    reel: ReelResponse
    status: AutomationStatusResponse


class BatchGenerateRequest(BaseModel):
    count: int = Field(..., ge=1, le=20)


class BatchGenerateResponse(BaseModel):
    message: str
    reels: list[ReelResponse]
    status: AutomationStatusResponse


class RootResponse(BaseModel):
    message: str
    docs_url: str
    health_url: str


# ── Async job polling ─────────────────────────────────────────────────────────

class GenerateJobResponse(BaseModel):
    """Returned immediately when a background reel-generation job is queued."""
    job_id: str
    status: str = "queued"
    message: str = "Reel generation started"


class SequentialBatchResponse(BaseModel):
    """
    Returned immediately when the /generate-async endpoint triggers a
    sequential batch of 3 reels (0 min / +10 min / +20 min offsets).
    Poll GET /jobs/{job_id} for each individual job's status and logs.
    """
    message: str
    job_ids: list[str]
    scheduled_at_utc: list[str]   # human-readable fire-time for each job
