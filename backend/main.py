import os

# Suppress noisy warnings before heavy imports
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")
os.environ.setdefault("ORT_DISABLE_ALL_LOGGING", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db
import models  # noqa: F401 — registers ALL ORM models with Base.metadata
from routes import automation, jobs, logs, reels, settings
from schemas import HealthResponse, RootResponse
from utils.storage import OUTPUT_DIR


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ── Database ──────────────────────────────────────────────────────────────
    try:
        init_db()
    except Exception as exc:
        print(f"[Startup] WARNING: DB init failed — {exc}")

    # ── Job recovery ──────────────────────────────────────────────────────────
    # Any job still "running"/"queued" from a previous session gets marked failed
    # so the frontend doesn't spin forever on stale job IDs.
    try:
        from services.job_service import recover_stale_jobs
        recovered = recover_stale_jobs()
        if recovered:
            print(f"[Startup] Recovered {recovered} stale job(s) → marked as failed")
    except Exception as exc:
        print(f"[Startup] WARNING: Job recovery failed — {exc}")

    # ── Storage cleanup ────────────────────────────────────────────────────────
    try:
        from utils.cleanup import run_full_cleanup
        summary = run_full_cleanup()
        if summary.get("total", 0):
            print(f"[Startup] Cleanup: removed {summary['clips']} clips, "
                  f"{summary['temp']} temp files")
    except Exception as exc:
        print(f"[Startup] WARNING: Cleanup failed — {exc}")

    # ── Ensure output directory exists ─────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[Startup] LOCAL PRODUCTION MODE — 720p / CRF22 / medium / ASS captions")
    print(f"[Startup] Reels saved to: {OUTPUT_DIR}")

    yield  # ── app is running ──


app = FastAPI(title="Reel Automation Dashboard API — Local", lifespan=lifespan)

# ── CORS — allow local Vite dev server ───────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve rendered reels as static files at /output/* ─────────────────────────
# Frontend uses <video src="http://localhost:8000/output/reel_*.mp4"> for playback.
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(automation.router)
app.include_router(jobs.router)
app.include_router(reels.router)
app.include_router(settings.router)
app.include_router(logs.router)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return RootResponse(
        message="Reel Automation Dashboard API — Local Production Mode",
        docs_url="/docs",
        health_url="/health",
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="ok")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "reel-local"}
