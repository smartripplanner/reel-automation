import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from routes import automation, jobs, logs, reels, settings
from schemas import HealthResponse, RootResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ── Database ────────────────────────────────────────────────────────────────
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        print(f"[Startup] WARNING: DB init failed — {exc}")

    yield  # ── app is running ──


app = FastAPI(title="Reel Automation Dashboard API", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Must be registered BEFORE any app.include_router() calls.
# Add extra origins via the CORS_ORIGIN env var (comma-separated) so new
# domains can be whitelisted on Render without a code change.
_extra = [o.strip() for o in os.getenv("CORS_ORIGIN", "").split(",") if o.strip()]

_CORS_ORIGINS = [
    # Local development
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # Production — Netlify frontend
    "https://bejewelled-hotteok-26a68c.netlify.app",
    # Any additional origins injected via CORS_ORIGIN env var (comma-separated)
    *_extra,
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # permissive during debug — tighten to _CORS_ORIGINS after confirming backend is up
    allow_credentials=False,    # must be False when allow_origins=["*"]
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
