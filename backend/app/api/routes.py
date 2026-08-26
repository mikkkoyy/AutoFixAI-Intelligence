from fastapi import APIRouter, HTTPException
from app.schemas import JobRequest, JobResponse, JobDetail, Event
from app.services.pipeline import PipelineService
from app.db import get_job, get_events

router = APIRouter(prefix="/api/v1")
pipeline = PipelineService()

@router.get("/status")
def status():
    return {
        "service": "autofix-ai-studio",
        "version": "1.0.0",
        "provider": pipeline.provider.name,
        "workflow": [
        "generate",
        "test",
        "diagnose",
        "fix",
        "retest",
        "verify",
        "checkpoint",
    ],
    }

@router.post("/jobs", response_model=JobResponse)
def create_job(request: JobRequest):
    job_id, _ = pipeline.run(request)
    row = get_job(job_id)
    return JobResponse(
        job_id=row["id"], name=row["name"], status=row["status"],
        attempts=row["attempts"], message=row["message"]
    )

@router.get("/jobs/{job_id}", response_model=JobDetail)
def job_detail(job_id: str):
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, "Job not found.")
    events = [
        Event(sequence=r["sequence"], event=r["event"], message=r["message"])
        for r in get_events(job_id)
    ]
    return JobDetail(
        job_id=row["id"], name=row["name"], task=row["task"],
        workspace=row["workspace"], status=row["status"],
        attempts=row["attempts"], message=row["message"], events=events
    )

@router.get("/jobs/{job_id}/events")
def job_events(job_id: str):
    if not get_job(job_id):
        raise HTTPException(404, "Job not found.")
    return {"job_id": job_id, "events": [
        {"sequence": r["sequence"], "event": r["event"], "message": r["message"]}
        for r in get_events(job_id)
    ]}
