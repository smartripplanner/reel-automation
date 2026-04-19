from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from routes import automation, jobs, logs, reels, settings
from schemas import HealthResponse, RootResponse
from services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Database initialisation
    init_db()

    # Auto-scheduler: triggers the pipeline 3× daily (09:00, 14:00, 19:00 UTC)
    # with a random South East Asia sub-topic.  Runs in a daemon background thread
    # so it never blocks the FastAPI event loop.
    start_scheduler()

    yield  # ── app is running ──

    # Graceful shutdown: stop APScheduler before process exits
    stop_scheduler()


app = FastAPI(title="Reel Automation Dashboard API", lifespan=lifespan)

import os as _os

_CORS_ORIGINS = [
    # Local development
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # Production — add your frontend Render/Vercel URL here (or set CORS_ORIGIN env var)
    *([_os.getenv("CORS_ORIGIN")] if _os.getenv("CORS_ORIGIN") else []),
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(automation.router)
app.include_router(jobs.router)
app.include_router(reels.router)
app.include_router(settings.router)
app.include_router(logs.router)


@app.get("/", response_model=RootResponse)
async def root():
    return RootResponse(
        message="Reel Automation Dashboard API is running",
        docs_url="/docs",
        health_url="/health",
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="ok")
