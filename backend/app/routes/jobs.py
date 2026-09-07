import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.director import run_pipeline
from app.db import SessionLocal, get_db
from app.schemas import JobCreate, JobOut
from app.services import job_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _run_in_background(job_id: str) -> None:
    # Background tasks get their own DB session — the request's session is
    # closed by the time this runs.
    db = SessionLocal()
    try:
        run_pipeline(db, job_id)
    finally:
        db.close()


@router.post("", response_model=JobOut)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not payload.brief.strip():
        raise HTTPException(status_code=400, detail="brief cannot be empty")
    job = job_service.create_job(db, payload.brief.strip())
    background_tasks.add_task(_run_in_background, job.id)
    return JobOut(
        id=job.id,
        brief=job.brief,
        status=job.status,
        error_message=job.error_message,
        result=None,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JobOut(
        id=job.id,
        brief=job.brief,
        status=job.status,
        error_message=job.error_message,
        result=job_service.job_result(job),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/{job_id}/stream")
async def stream_job(job_id: str):
    """Server-sent events of agent progress. Polls the DB every 400ms — simple
    and correct for a single backend instance. If this ever runs as multiple
    instances behind a load balancer, swap this loop for a Redis pub/sub
    subscription instead; the event shape emitted to the client doesn't change.
    """

    async def event_generator():
        last_event_id = None
        while True:
            db = SessionLocal()
            try:
                job = job_service.get_job(db, job_id)
                if not job:
                    yield f"event: error\ndata: {json.dumps({'detail': 'job not found'})}\n\n"
                    return

                new_events = job_service.get_events_since(db, job_id, last_event_id)
                for ev in new_events:
                    last_event_id = ev.id
                    payload = {"agent_key": ev.agent_key, "note": ev.note}
                    yield f"data: {json.dumps(payload)}\n\n"

                if job.status in ("done", "error"):
                    final_payload = {
                        "status": job.status,
                        "error_message": job.error_message,
                        "result": job_service.job_result(job),
                    }
                    yield f"event: final\ndata: {json.dumps(final_payload)}\n\n"
                    return
            finally:
                db.close()
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
