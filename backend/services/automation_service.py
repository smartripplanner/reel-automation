import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from automation.main_pipeline import run_pipeline
from database import SessionLocal
from schemas import AutomationStatusResponse, GenerateJobResponse, SequentialBatchResponse
from services import job_service
from services.reel_service import create_reel
from services.settings_service import get_or_create_settings
from utils.logger import log_message, log_message_safe


automation_state = {
    "is_running": False,
    "mode": "manual",
    "last_run_at": None,
    "active_job": None,
}
automation_task: asyncio.Task | None = None


def _build_caption() -> str:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return f"Auto-generated reel created at {timestamp} UTC"


def _generate_reel_record(db: Session, trigger_message: str) -> dict:
    automation_state["active_job"] = "reel_generation"
    settings = get_or_create_settings(db)
    log_message(db, trigger_message)

    try:
        pipeline_result = run_pipeline(
            category_hint=settings.niche,
            log_handler=log_message_safe,   # thread-safe: own session per write
        )
        reel_status = pipeline_result["status"]
        file_path = pipeline_result.get("file_path") or "storage/reels/unavailable.mp4"
        caption = pipeline_result.get("caption") or _build_caption()

        if reel_status == "failed":
            error_message = pipeline_result.get("error", "Unknown error")
            log_message(db, f"Reel generation failed: {error_message}")
            message = "Reel generation failed"
        else:
            log_message(db, f"Reel saved to {file_path}")
            message = "Reel generated successfully"

        reel = create_reel(
            db=db,
            file_path=file_path,
            caption=caption,
            status=reel_status,
        )

        automation_state["last_run_at"] = datetime.utcnow()
        automation_state["active_job"] = None
        return {
            "message": message,
            "reel": reel,
            "status": AutomationStatusResponse(**automation_state),
        }
    finally:
        automation_state["active_job"] = None


async def _automation_loop() -> None:
    try:
        while automation_state["is_running"]:
            db = SessionLocal()
            try:
                settings = get_or_create_settings(db)
                _generate_reel_record(db, "Automated reel generation triggered")
                interval_seconds = max(int(86400 / max(settings.reels_per_day, 1)), 60)
            finally:
                db.close()

            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        pass


async def start_automation(db: Session) -> dict:
    global automation_task

    if automation_state["is_running"]:
        return automation_state

    automation_state["is_running"] = True
    automation_state["mode"] = "automated"
    automation_state["active_job"] = "automation_loop"
    log_message(db, "Automation started")
    automation_task = asyncio.create_task(_automation_loop())
    return automation_state


async def stop_automation(db: Session) -> dict:
    global automation_task

    automation_state["is_running"] = False
    automation_state["mode"] = "manual"
    automation_state["active_job"] = None

    if automation_task:
        automation_task.cancel()
        automation_task = None

    log_message(db, "Automation stopped")
    return automation_state


def generate_reel(db: Session) -> dict:
    return _generate_reel_record(db, "Manual reel generation triggered")


def generate_batch_reels(db: Session, count: int) -> dict:
    generated_reels = [None] * count
    automation_state["active_job"] = f"batch_generation_{count}"
    log_message(db, f"Batch reel generation triggered for {count} reels")

    def run_single_reel(index: int):
        worker_db = SessionLocal()
        try:
            log_message(worker_db, f"Starting reel {index + 1} of {count}")
            result = _generate_reel_record(worker_db, f"Manual reel generation triggered ({index + 1}/{count})")
            log_message(worker_db, f"Finished reel {index + 1} of {count} with status {result['reel'].status}")
            return index, result["reel"]
        except Exception as exc:
            failed_reel = create_reel(
                db=worker_db,
                file_path="storage/reels/unavailable.mp4",
                caption=f"Batch reel {index + 1} failed",
                status="failed",
            )
            log_message(worker_db, f"Batch reel {index + 1} failed with error: {exc}")
            return index, failed_reel
        finally:
            worker_db.close()

    try:
        max_workers = min(max(count, 1), 3)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_single_reel, index) for index in range(count)]
            for future in as_completed(futures):
                index, reel = future.result()
                generated_reels[index] = reel

        automation_state["last_run_at"] = datetime.utcnow()
        automation_state["active_job"] = None
        return {
            "message": f"Batch generation completed for {count} reels",
            "reels": [reel for reel in generated_reels if reel is not None],
            "status": AutomationStatusResponse(**automation_state),
        }
    finally:
        automation_state["active_job"] = None


def get_status() -> AutomationStatusResponse:
    return AutomationStatusResponse(**automation_state)


# ─────────────────────────────────────────────────────────────────────────────
# Async job — returns job_id immediately, runs pipeline in background thread
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline_as_job(job_id: str, topic_override: str | None = None) -> None:
    """
    Background thread: run the full pipeline and store the result in job_service.

    Parameters
    ----------
    job_id         : ID of the job record created by job_service.create_job()
    topic_override : Optional explicit topic string injected by the APScheduler.
                     When set, bypasses settings.niche and uses this topic directly.
                     Allows the scheduler to pass specific South East Asia sub-topics
                     without modifying the user's saved settings.

    Thread-safety note
    ──────────────────
    run_pipeline() uses ThreadPoolExecutor internally (Stage 2: audio + media
    fetch run in parallel).  Both worker threads call log_handler(), which
    previously wrote to a shared SQLAlchemy session via db.commit().

    Root cause of the crash: SQLAlchemy 2.0 sessions are NOT thread-safe.
    A shared session that starts a commit on thread A can land in a CLOSED /
    COMMITTING state that poisons all subsequent commit() calls — even those
    waiting behind a threading.Lock — producing the misleading error:
        "Method 'commit()' can't be called here; already in progress"

    Fix: log_message_safe() opens a brand-new session for every log write and
    closes it before returning, so no session state is ever shared between
    threads.  A module-level lock inside log_message_safe serialises the
    SQLite writes without any shared-session risk.  The main `db` session is
    reserved for the non-parallel parts of the function (settings read and
    reel record creation).
    """
    job_service.set_running(job_id)

    db = SessionLocal()

    try:
        settings = get_or_create_settings(db)

        # topic_override (from scheduler) takes priority over saved settings.niche
        category_hint = topic_override or settings.niche

        def _log(msg: str) -> None:
            # Forward to in-memory job log immediately (no lock needed)
            job_service.append_log(job_id, msg)
            # Each DB write gets its own isolated session — fully thread-safe
            log_message_safe(msg)

        if topic_override:
            _log(f"[Scheduler] Topic override active: '{topic_override}'")

        pipeline_result = run_pipeline(
            category_hint=category_hint,
            log_handler=_log,
        )

        file_path = pipeline_result.get("file_path") or "storage/reels/unavailable.mp4"
        caption = pipeline_result.get("caption") or "Auto-generated reel"
        status = pipeline_result.get("status", "completed")

        reel = create_reel(db=db, file_path=file_path, caption=caption, status=status)

        automation_state["last_run_at"] = datetime.utcnow()
        job_service.set_completed(job_id, {
            "status": status,
            "file_path": file_path,
            "caption": caption,
            "topic": pipeline_result.get("topic", ""),
            "reel_id": reel.id,
        })
    except Exception as exc:
        job_service.set_failed(job_id, str(exc))
    finally:
        db.close()


def run_generate_job(background_tasks: BackgroundTasks, db: Session) -> GenerateJobResponse:
    """
    Queue a reel-generation job.  Returns immediately with job_id.
    The caller polls GET /jobs/{job_id} for updates.
    """
    job_id = job_service.create_job()
    log_message(db, f"Async reel job queued: {job_id}")
    background_tasks.add_task(_run_pipeline_as_job, job_id)
    return GenerateJobResponse(job_id=job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Sequential batch trigger — 3 reels, completion-chained
# ─────────────────────────────────────────────────────────────────────────────

def _run_sequential_batch_jobs(
    job_ids: list[str],
    topic_overrides: list[str | None],
) -> None:
    """
    Worker thread: execute pipeline jobs one-at-a-time.

    Reel N+1 starts only AFTER Reel N has fully completed — including
    FFmpeg render, S3 upload, and Instagram publish.  No time-based
    scheduling; completion drives progression.

    topic_overrides are pre-generated by trigger_sequential_batch() using
    generate_unique_topics() / random.sample(), guaranteeing that all 3
    reels in a batch cover distinct topics — no repeated random.choice()
    collision possible.
    """
    total = len(job_ids)
    try:
        for slot, (job_id, topic) in enumerate(zip(job_ids, topic_overrides), start=1):
            automation_state["active_job"] = f"batch_{slot}/{total}_{job_id[:8]}"
            log_message_safe(
                f"[SequentialBatch] ▶ Starting reel {slot}/{total} "
                f"— job_id={job_id} | topic='{topic}'"
            )
            try:
                _run_pipeline_as_job(job_id, topic)   # blocking — returns only after IG publish
            except Exception as exc:
                log_message_safe(
                    f"[SequentialBatch] Reel {slot}/{total} error (non-fatal): {exc}"
                )
            status = (job_service.get_job(job_id) or {}).get("status", "unknown")
            log_message_safe(
                f"[SequentialBatch] ✓ Reel {slot}/{total} done — status={status}"
            )
    finally:
        # Always reset state when the batch finishes or crashes
        automation_state["is_running"] = False
        automation_state["mode"] = "manual"
        automation_state["active_job"] = None
        log_message_safe("[SequentialBatch] Batch complete — automation idle")


def _dispatch_github_workflow(topic: str) -> dict:
    """
    Fire a single workflow_dispatch event against GitHub Actions.

    Returns a result dict:
        {"status": "dispatched", "runs_url": "...", "topic": "..."}
        {"status": "error",      "message":  "..."}                    — on failure

    GitHub responds with HTTP 204 No Content on success — there is no
    run-ID in the response body.  The UI link points to the workflow's
    run list, not a specific run, because the run ID is only available
    by polling GET /repos/{owner}/{repo}/actions/runs after dispatch.

    Required .env / GitHub Secrets:
        GITHUB_PAT    — Personal Access Token, scope: workflow
        GITHUB_REPO   — e.g., "yourname/reel-automation-dashboard"
        GITHUB_BRANCH — branch to run on (default: "main")
    """
    import requests as _requests  # lazy import — not needed by local-only path

    gh_pat    = os.getenv("GITHUB_PAT", "").strip()
    gh_repo   = os.getenv("GITHUB_REPO", "").strip()
    gh_branch = os.getenv("GITHUB_BRANCH", "main").strip() or "main"

    if not gh_pat:
        return {"status": "error", "message": "GITHUB_PAT not set in .env — cannot dispatch workflow"}
    if not gh_repo:
        return {"status": "error", "message": "GITHUB_REPO not set in .env (e.g. 'yourname/reel-automation-dashboard')"}

    url = (
        f"https://api.github.com/repos/{gh_repo}"
        f"/actions/workflows/generate_reel.yml/dispatches"
    )
    headers = {
        "Authorization":        f"Bearer {gh_pat}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref":    gh_branch,
        "inputs": {"topic": topic},
    }

    try:
        resp = _requests.post(url, headers=headers, json=payload, timeout=15)
    except _requests.exceptions.Timeout:
        return {"status": "error", "message": "GitHub API request timed out (15 s)"}
    except _requests.exceptions.RequestException as exc:
        return {"status": "error", "message": f"GitHub API request failed: {exc}"}

    if resp.status_code == 204:
        # 204 No Content — dispatch accepted, workflow queued
        runs_url = f"https://github.com/{gh_repo}/actions/workflows/generate_reel.yml"
        return {"status": "dispatched", "runs_url": runs_url, "topic": topic}

    if resp.status_code == 404:
        return {
            "status": "error",
            "message": (
                f"Workflow not found (404). Check GITHUB_REPO='{gh_repo}' "
                f"and that generate_reel.yml exists on branch '{gh_branch}'."
            ),
        }

    if resp.status_code == 401:
        return {
            "status": "error",
            "message": "GitHub PAT rejected (401 Unauthorized). Verify GITHUB_PAT has 'workflow' scope.",
        }

    # Any other non-2xx
    detail = resp.text[:300] if resp.text else "(empty body)"
    return {
        "status":      "error",
        "message":     f"GitHub API returned HTTP {resp.status_code}",
        "detail":      detail,
    }


def trigger_sequential_batch(db: Session) -> SequentialBatchResponse:
    """
    Dispatch 3 reel-generation jobs to GitHub Actions (Hybrid Serverless).

    Architecture
    ────────────
    FastAPI (Render) = Brain  → calls this function, returns immediately
    GitHub Actions   = Muscle → runs github_worker.py for each topic

    Each dispatch is an independent workflow_dispatch event against
    generate_reel.yml.  GitHub Actions handles FFmpeg rendering, S3 upload,
    and Instagram publishing — no local thread, no Render RAM consumed.

    Topics are pre-generated via random.sample() so all 3 reels are
    guaranteed to cover distinct content angles.

    Job lifecycle
    ─────────────
    "queued"     → job created, dispatch pending
    "dispatched" → GitHub accepted the event (HTTP 204)
    "failed"     → GitHub rejected the dispatch (bad PAT / missing repo)

    Monitor actual render progress at:
        https://github.com/{GITHUB_REPO}/actions/workflows/generate_reel.yml
    """
    from automation.topic_engine import generate_unique_topics

    # ── Pre-generate 3 unique topics via random.sample ────────────────────────
    settings = get_or_create_settings(db)
    topics: list[str] = generate_unique_topics(settings.niche, count=3)

    job_ids: list[str] = []
    scheduled_times: list[str] = []

    for slot, topic in enumerate(topics, start=1):
        job_id = job_service.create_job()
        job_ids.append(job_id)

        log_message(
            db,
            f"Batch reel {slot}/3 — dispatching to GitHub Actions | topic='{topic}'",
        )
        job_service.append_log(
            job_id,
            f"[GitHub] Dispatching workflow for topic='{topic}' (reel {slot}/3)",
        )

        # ── Fire workflow_dispatch event ──────────────────────────────────────
        result = _dispatch_github_workflow(topic)

        if result["status"] == "dispatched":
            runs_url = result.get("runs_url", "")
            job_service.append_log(job_id, f"[GitHub] ✅ Workflow dispatched — monitor: {runs_url}")
            log_message(db, f"Batch reel {slot}/3 dispatched | topic='{topic}' | {runs_url}")
            job_service.set_completed(job_id, {
                "status":          "dispatched",
                "topic":           topic,
                "github_runs_url": runs_url,
                "message":         "Reel generation dispatched to GitHub Actions",
            })
            scheduled_times.append("dispatched to GitHub Actions")

        else:
            error_msg = result.get("message") or result.get("detail") or "Unknown dispatch error"
            job_service.append_log(job_id, f"[GitHub] ❌ Dispatch failed: {error_msg}")
            log_message(db, f"Batch reel {slot}/3 dispatch failed: {error_msg}")
            job_service.set_failed(job_id, f"GitHub dispatch failed: {error_msg}")
            scheduled_times.append(f"dispatch failed: {error_msg[:60]}")

    # Automation is non-blocking — FastAPI returns immediately after dispatch
    automation_state["is_running"] = False
    automation_state["mode"]       = "manual"
    automation_state["active_job"] = None
    automation_state["last_run_at"] = datetime.utcnow()

    dispatched = sum(1 for t in scheduled_times if t == "dispatched to GitHub Actions")
    log_message(
        db,
        f"Hybrid batch complete — {dispatched}/3 workflows dispatched to GitHub Actions",
    )

    return SequentialBatchResponse(
        message=f"Batch of 3 dispatched to GitHub Actions ({dispatched}/3 succeeded)",
        job_ids=job_ids,
        scheduled_at_utc=scheduled_times,
    )
