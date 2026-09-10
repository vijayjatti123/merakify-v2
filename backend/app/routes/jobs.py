import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.director import SHOT_STATUS_PENDING, _attach_voice_refs, run_pipeline, validate_and_correct
from app.db import SessionLocal, get_db
from app.schemas import AssetOut, AssetRole, JobCreate, JobOut, JobRetry, JobRevise
from app.services import asset_service, job_service, storage_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
assets_router = APIRouter(prefix="/api/assets", tags=["assets"])

RETRY_CONTEXT_MARKER = "\n\n--- Retry context ---\n"


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
        aspect_ratio=job.aspect_ratio,
        quality=job.quality,
        language=job.language,
        ai_model=job.ai_model,
        status=job.status,
        error_message=job.error_message,
        result=job_service.job_result(job) if result is None else result,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _create_and_start_job(
    brief: str,
    background_tasks: BackgroundTasks,
    db: Session,
    *,
    aspect_ratio: str = "16:9",
    quality: str = "720p",
    language: str = "English",
    ai_model: str = "Seedance 2.5",
) -> JobOut:
    job = job_service.create_job(
        db,
        brief,
        aspect_ratio=aspect_ratio,
        quality=quality,
        language=language,
        ai_model=ai_model,
    )
    background_tasks.add_task(_run_in_background, job.id)
    return _job_out(job)


def _retry_brief(job, change_request: str | None) -> str:
    """Build one retry prompt without carrying earlier retry context forward."""
    original_brief = job.brief.split(RETRY_CONTEXT_MARKER, 1)[0].strip()
    result = job_service.job_result(job) or {}
    previous_script = result.get("script")

    context = [
        "Preserve every hard requirement in the original brief, including product, duration, language, format, and any required dialogue.",
        "Create an unmistakably different script and creative treatment from the previous version, not a paraphrase or a reshuffling of the same beats.",
    ]
    if previous_script:
        context.append(
            "Use this immediately preceding script only as a do-not-repeat reference: "
            f"{json.dumps(previous_script, ensure_ascii=False)}"
        )
    if change_request and change_request.strip():
        context.append(f"The user specifically requested this change: {change_request.strip()}")
        context.append(
            "Make that requested change the organizing idea of the new hook, setting, scene progression, visual actions, and ending."
        )
    else:
        context.append(
            "The user gave no specific change request. Silently choose a contrasting creative premise, then change at least four unconstrained axes among setting, time of day, mood, point of view, central metaphor, character action, narrative structure, and ending."
        )
    context.append(
        "Do not reuse the previous logline, scene headings, scene descriptions, action sequence, visual hook, or closing beat."
    )

    return f"{original_brief}{RETRY_CONTEXT_MARKER}{' '.join(context)}"


@router.post("", response_model=JobOut)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not payload.brief.strip():
        raise HTTPException(status_code=400, detail="brief cannot be empty")
    if not payload.language.strip():
        raise HTTPException(status_code=400, detail="language cannot be empty")
    return _create_and_start_job(
        payload.brief.strip(),
        background_tasks,
        db,
        aspect_ratio=payload.aspect_ratio,
        quality=payload.quality,
        language=payload.language.strip(),
        ai_model=payload.ai_model,
    )


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
    for shot in validated_shots:
        shot["status"] = SHOT_STATUS_PENDING

    updated_result = dict(result)
    updated_result.update(
        shots=validated_shots,
        generation_approved=False,
        qa=validated["qa"],
        assembly=validated["assembly"],
    )
    job_service.set_result(db, job_id, updated_result)
    db.refresh(job)
    return _job_out(job, updated_result)


@router.post("/{job_id}/approve", response_model=JobOut)
def approve_job(job_id: str, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail="only completed jobs can be approved")

    result = job_service.job_result(job)
    if not result:
        raise HTTPException(status_code=409, detail="completed job has no stored result")

    updated_result = dict(result)
    updated_result["generation_approved"] = True
    updated_result["shots"] = [
        {**shot, "status": SHOT_STATUS_PENDING} for shot in result.get("shots", [])
    ]
    job_service.set_result(db, job_id, updated_result)
    db.refresh(job)
    return _job_out(job, updated_result)


@router.post("/{job_id}/shots/{shot_number}/regenerate", response_model=JobOut)
def regenerate_shot(job_id: str, shot_number: int, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail="only completed jobs can regenerate shots")

    result = job_service.job_result(job)
    if not result:
        raise HTTPException(status_code=409, detail="completed job has no stored result")
    if not result.get("generation_approved"):
        raise HTTPException(status_code=409, detail="approve the shot list before regenerating a shot")

    found = False
    updated_shots = []
    for shot in result.get("shots", []):
        if shot.get("shot_number") == shot_number:
            updated_shots.append({**shot, "status": SHOT_STATUS_PENDING})
            found = True
        else:
            updated_shots.append(dict(shot))
    if not found:
        raise HTTPException(status_code=404, detail="shot not found")

    updated_result = dict(result)
    updated_result["shots"] = updated_shots
    job_service.set_result(db, job_id, updated_result)
    db.refresh(job)
    return _job_out(job, updated_result)


@router.post("/{job_id}/retry")
def retry_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    payload: JobRetry | None = None,
    db: Session = Depends(get_db),
):
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    enriched_brief = _retry_brief(job, payload.change_request if payload else None)
    retried = _create_and_start_job(
        enriched_brief,
        background_tasks,
        db,
        aspect_ratio=job.aspect_ratio,
        quality=job.quality,
        language=job.language,
        ai_model=job.ai_model,
    )
    return {"id": retried.id}


@assets_router.post("/upload", response_model=AssetOut)
def upload_asset(
    file: UploadFile = File(...),
    role: AssetRole | None = Form(None),
    label: str | None = Form(None, max_length=120),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "asset").name.strip() or "asset"
    normalized_label = label.strip() if label and label.strip() else None
    object_key = f"assets/{uuid.uuid4()}/{filename}"
    uploaded = storage_service.upload_file(
        object_key,
        file.file,
        content_type=file.content_type,
    )
    try:
        asset = asset_service.create_asset(
            db,
            filename=filename,
            object_key=uploaded["key"],
            url=uploaded["url"],
            role=role,
            label=normalized_label,
        )
    except Exception:
        storage_service.delete_object(uploaded["key"])
        raise
    return AssetOut(
        id=asset.id,
        filename=asset.filename,
        url=uploaded["url"],
        role=asset.role,
        label=asset.label,
        created_at=asset.created_at,
    )


@assets_router.get("", response_model=list[AssetOut])
def list_assets(db: Session = Depends(get_db)):
    return [
        AssetOut(
            id=asset.id,
            filename=asset.filename,
            url=storage_service.asset_url(asset.object_key),
            role=asset.role,
            label=asset.label,
            created_at=asset.created_at,
        )
        for asset in asset_service.list_assets(db)
    ]


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
