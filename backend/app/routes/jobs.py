import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.director import _attach_voice_refs, run_pipeline, validate_and_correct
from app.db import SessionLocal, get_db
from app.schemas import JobCreate, JobOut, JobRevise
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


def _job_out(job, result=None) -> JobOut:
    return JobOut(
        id=job.id,
        brief=job.brief,
        status=job.status,
        error_message=job.error_message,
        result=job_service.job_result(job) if result is None else result,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _create_and_start_job(brief: str, background_tasks: BackgroundTasks, db: Session) -> JobOut:
    job = job_service.create_job(db, brief)
    background_tasks.add_task(_run_in_background, job.id)
    return _job_out(job)


@router.post("", response_model=JobOut)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not payload.brief.strip():
        raise HTTPException(status_code=400, detail="brief cannot be empty")
    return _create_and_start_job(payload.brief.strip(), background_tasks, db)


@router.post("/{job_id}/revise", response_model=JobOut)
def revise_job(job_id: str, payload: JobRevise, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail="only completed jobs can be revised")

    result = job_service.job_result(job)
    if not result:
        raise HTTPException(status_code=409, detail="completed job has no stored result")

    edits_by_number = {edit.shot_number: edit for edit in payload.shots}
    if len(edits_by_number) != len(payload.shots):
        raise HTTPException(status_code=400, detail="shot_number values must be unique")

    stored_numbers = {shot["shot_number"] for shot in result["shots"]}
    unknown_numbers = set(edits_by_number) - stored_numbers
    if unknown_numbers:
        raise HTTPException(status_code=400, detail="edited shot_number was not found in this job")

    merged_shots = []
    for shot in result["shots"]:
        merged = dict(shot)
        edit = edits_by_number.get(shot["shot_number"])
        if edit:
            merged["dialogue_text"] = edit.dialogue_text
            merged["description"] = edit.description
        merged_shots.append(merged)

    continuity = result["continuity"]
    target_duration_sec = result["format"]["duration_target_sec"]
    validated = validate_and_correct(
        merged_shots,
        continuity["characters"],
        target_duration_sec,
        narrator_voice_ref=continuity.get("narrator_voice_ref"),
    )
    validated_shots = _attach_voice_refs(
        validated["shots"], continuity["characters"], continuity.get("narrator_voice_ref")
    )

    updated_result = dict(result)
    updated_result.update(
        shots=validated_shots,
        qa=validated["qa"],
        assembly=validated["assembly"],
    )
    job_service.set_result(db, job_id, updated_result)
    db.refresh(job)
    return _job_out(job, updated_result)


@router.post("/{job_id}/retry")
def retry_job(job_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    retried = _create_and_start_job(job.brief, background_tasks, db)
    return {"id": retried.id}


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_out(job)


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
