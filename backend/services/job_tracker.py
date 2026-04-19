import uuid
from datetime import datetime

jobs = {}


def create_job(topic: str):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "topic": topic,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }
    return jobs[job_id]


def update_job(job_id, status):
    if job_id in jobs:
        jobs[job_id]["status"] = status


def get_job(job_id):
    return jobs.get(job_id)


def get_all_jobs():
    return list(jobs.values())
