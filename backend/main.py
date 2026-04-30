import os

# Suppress ONNX Runtime GPU-discovery warning on CPU-only servers (Render free tier).
# Must be set before any onnxruntime / faster-whisper import.
os.environ.setdefault("ORT_LOGGING_LEVEL", "3")          # ERROR level only
os.environ.setdefault("ORT_DISABLE_ALL_LOGGING", "1")    # belt-and-suspenders

# Suppress HuggingFace Hub unauthenticated-request warning when HF_TOKEN is absent.
# The model still downloads fine; this just removes the noisy stderr line.
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import init_db
import models  # noqa: F401 — registers ALL ORM models (incl. Job) with Base.metadata
from routes import automation, export, jobs, logs, reels, settings
from schemas import HealthResponse, RootResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ── Database ────────────────────────────────────────────────────────────────
    try:
        # init_db() creates all tables, enables WAL mode, and runs column migration
        init_db()
    except Exception as exc:
        print(f"[Startup] WARNING: DB init failed — {exc}")

    # ── Job recovery ────────────────────────────────────────────────────────────
    # Any job still "running" or "queued" from the previous process is now
    # unreachable (worker subprocess gone). Mark them failed so the frontend
    # stops polling with 404s and shows a clear error message instead.
    try:
        from services.job_service import recover_stale_jobs
        recovered = recover_stale_jobs()
        if recovered:
            print(f"[Startup] Recovered {recovered} stale job(s) → marked as failed")
    except Exception as exc:
        print(f"[Startup] WARNING: Job recovery failed — {exc}")

    # ── Storage cleanup ──────────────────────────────────────────────────────────
    # Remove orphan clips, temp segments, and old reels from crashed previous runs.
    try:
        from utils.cleanup import run_full_cleanup
        summary = run_full_cleanup()
        if summary.get("total", 0):
            print(f"[Startup] Cleanup: removed {summary['clips']} clips, "
                  f"{summary['reels']} old reels, {summary['temp']} temp files")
    except Exception as exc:
        print(f"[Startup] WARNING: Cleanup failed — {exc}")

    # ── Log plan mode ─────────────────────────────────────────────────────────────
    free_plan = os.getenv("FREE_PLAN", "false").lower() in ("true", "1", "yes")
    print(f"[Startup] Plan: {'FREE_PLAN — 480p / veryfast / SRT captions' if free_plan else 'STANDARD — 720p / ultrafast / ASS captions'}")

    yield  # ── app is running ──


app = FastAPI(title="Reel Automation Dashboard API", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
_extra = [o.strip() for o in os.getenv("CORS_ORIGIN", "").split(",") if o.strip()]

_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://bejewelled-hotteok-26a68c.netlify.app",
    *_extra,
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten to _CORS_ORIGINS once backend is confirmed stable
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(automation.router)
app.include_router(export.router)
app.include_router(jobs.router)
app.include_router(reels.router)
app.include_router(settings.router)
app.include_router(logs.router)


# ── Root — supports both GET and HEAD ─────────────────────────────────────────
# Render's health check probes HEAD / on startup; without an explicit HEAD
# handler FastAPI returns 405, which fails the health check and prevents the
# service from being marked "live". Both methods return the same JSON body.
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return RootResponse(
        message="Reel Automation Dashboard API is running",
        docs_url="/docs",
        health_url="/health",
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="ok")


# /healthz alias — some infra tooling (GCP, k8s) expects this path
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "reel-backend"}
