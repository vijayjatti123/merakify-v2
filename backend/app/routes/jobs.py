import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents import prompts
from app.agents.llm_client import call_agent
from app.agents.director import (
    SHOT_STATUS_GENERATING,
    SHOT_STATUS_PENDING,
    _attach_voice_refs,
    run_pipeline,
    validate_and_correct,
)
from app.db import SessionLocal, get_db
from app.schemas import JobCreate, JobOut, JobRetry, JobRevise, ShotRegenerateHints
from app.services import job_service, voice_generation_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

RETRY_CONTEXT_MARKER = "\n\n--- Retry context ---\n"


def _run_in_background(job_id: str) -> None:
    # Background tasks get their own DB session — the request's session is
    # closed by the time this runs.
    db = SessionLocal()
    try:
        run_pipeline(db, job_id)
    finally:
        db.close()


def _run_voice_generation_in_background(job_id: str, shot_numbers: set[int] | None = None) -> None:
    db = SessionLocal()
    try:
        asyncio.run(
            voice_generation_service.generate_job_dialogue_audio(
                db,
                job_id,
                shot_numbers=shot_numbers,
            )
        )
    except Exception as error:  # noqa: BLE001 - persist task-level failures on affected shots
        voice_generation_service.fail_unfinished_shots(
            db,
            job_id,
            error,
            shot_numbers=shot_numbers,
        )
    finally:
        db.close()


def _job_out(job, result=None) -> JobOut:
    return JobOut(
        id=job.id,
        brief=job.brief,
        aspect_ratio=job.aspect_ratio,
        visual_style=job.visual_style,
        color_grade=job.color_grade,
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
    visual_style: str | None = None,
    color_grade: str | None = None,
    quality: str = "720p",
    language: str = "English",
    ai_model: str = "Seedance 2.5",
    script_text: str | None = None,
    resolutions: dict | None = None,
) -> JobOut:
    job = job_service.create_job(
        db,
        brief,
        aspect_ratio=aspect_ratio,
        visual_style=visual_style,
        color_grade=color_grade,
        quality=quality,
        language=language,
        ai_model=ai_model,
        script_text=script_text,
        resolutions=resolutions,
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
        visual_style=payload.visual_style if "visual_style" in payload.model_fields_set else None,
        color_grade=payload.color_grade if "color_grade" in payload.model_fields_set else None,
        quality=payload.quality,
        language=payload.language.strip(),
        ai_model=payload.ai_model,
        script_text=payload.script_text.strip() if payload.script_text is not None else None,
        resolutions=payload.resolutions.model_dump() if payload.resolutions is not None else None,
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
        source_script_text=result.get("source_script_text"),
        emit=lambda key, note: job_service.append_event(db, job_id, key, note),
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
def approve_job(job_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
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
    updated_result["audio_assembly_pending"] = any(shot.get("has_dialogue") for shot in result.get("shots", []))
    updated_result["shots"] = [
        {
            **shot,
            "status": SHOT_STATUS_PENDING,
            "error_message": None,
            "dialogue_audio_url": None,
            "dialogue_audio_provider": None,
        }
        for shot in result.get("shots", [])
    ]
    job_service.set_result(db, job_id, updated_result)
    job_service.append_event(db, job_id, "voice_generation", "Approved; starting real dialogue voice generation.")
    for shot in updated_result["shots"]:
        job_service.append_shot_status_event(
            db,
            job_id,
            shot["shot_number"],
            SHOT_STATUS_PENDING,
            message=f"Shot {shot['shot_number']} queued for voice generation.",
        )
    background_tasks.add_task(_run_voice_generation_in_background, job_id)
    db.refresh(job)
    return _job_out(job, updated_result)


@router.post("/{job_id}/shots/{shot_number}/regenerate", response_model=JobOut)
def regenerate_shot(
    job_id: str,
    shot_number: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    payload: ShotRegenerateHints | None = None,
):
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
            updated_shots.append(
                {
                    **shot,
                    "status": SHOT_STATUS_PENDING,
                    "error_message": None,
                    "dialogue_audio_url": None,
                    "dialogue_audio_provider": None,
                }
            )
            found = True
        else:
            updated_shots.append(dict(shot))
    if not found:
        raise HTTPException(status_code=404, detail="shot not found")

    updated_result = dict(result)
    hints = payload.model_dump(exclude_none=True) if payload else {}
    if hints:
        visual_fields = ("camera_angle", "camera_movement", "lens", "lighting", "composition_note")
        try:
            cine = call_agent(
                prompts.CINEMATOGRAPHY_FIX,
                f"Current shots: {json.dumps(result['shots'])}\n"
                f"Target shot number: {shot_number}\nOptional style hints: {json.dumps(hints)}",
                max_tokens=4096,
            )
            candidates = [shot for shot in cine["shots"] if shot["shot_number"] == shot_number]
            if len(candidates) != 1:
                raise ValueError("Expected exactly one target shot")
            candidate = candidates[0]
            if any(not isinstance(candidate.get(key), str) or not candidate[key].strip() for key in visual_fields):
                raise ValueError("Missing visual fields")
            # Copy only visual fields: dialogue, timing, identity and all neighbors remain authoritative.
            proposed = [
                {**shot, **{key: candidate[key] for key in visual_fields}} if shot["shot_number"] == shot_number else dict(shot)
                for shot in result["shots"]
            ]
            continuity = result["continuity"]
            validated = validate_and_correct(
                proposed,
                continuity["characters"],
                result["format"]["duration_target_sec"],
                narrator_voice_ref=continuity.get("narrator_voice_ref"),
            )
            if not validated["qa"].get("approved"):
                raise ValueError("Continuity check did not approve the hint")
            checked = validated["shots"]
            if len(checked) != len(proposed):
                raise ValueError("Correction changed the shot list")
            protected_fields = ("shot_number", "scene_number", "duration_sec", "description", "characters_in_shot", "has_dialogue", "dialogue_text")
            for original, revised in zip(proposed, checked):
                fields = protected_fields if original["shot_number"] == shot_number else (*protected_fields, *visual_fields)
                for key in fields:
                    if revised.get(key) != original.get(key):
                        raise ValueError("Correction changed protected shot fields")
            target = next(shot for shot in checked if shot["shot_number"] == shot_number)
            if any(not isinstance(target.get(key), str) or not target[key].strip() for key in visual_fields):
                raise ValueError("Correction returned invalid visual fields")
            for shot in updated_shots:
                if shot["shot_number"] == shot_number:
                    shot.update({key: target[key] for key in visual_fields})
            updated_result.update(qa=validated["qa"], assembly=validated["assembly"])
        except Exception as error:
            # Do not expose provider errors containing raw briefs or leave a partial change stored.
            raise HTTPException(status_code=502, detail="Style hint could not be applied safely; shot unchanged. Retry or clear the hints.") from error
    updated_result["shots"] = updated_shots
    updated_result["audio_assembly_pending"] = any(shot.get("has_dialogue") for shot in updated_shots)
    job_service.set_result(db, job_id, updated_result)
    job_service.append_shot_status_event(
        db,
        job_id,
        shot_number,
        SHOT_STATUS_PENDING,
        message=f"Shot {shot_number} queued for regeneration.",
    )
    background_tasks.add_task(_run_voice_generation_in_background, job_id, {shot_number})
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
        visual_style=job.visual_style,
        color_grade=job.color_grade,
        quality=job.quality,
        language=job.language,
        ai_model=job.ai_model,
    )
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
                    payload = {
                        "agent_key": ev.agent_key,
                        "note": ev.note,
                        "created_at": ev.created_at.isoformat(),
                    }
                    if ev.agent_key == "shot_status":
                        try:
                            shot_event = json.loads(ev.note)
                        except json.JSONDecodeError:
                            shot_event = {}
                        payload.update(shot_event)
                        payload["note"] = shot_event.get("message", ev.note)
                    yield f"data: {json.dumps(payload)}\n\n"

                result = job_service.job_result(job)
                generation_running = bool(
                    job.status == "done"
                    and result
                    and result.get("generation_approved")
                    and (result.get("audio_assembly_pending") or any(
                        shot.get("status") in {SHOT_STATUS_PENDING, SHOT_STATUS_GENERATING}
                        for shot in result.get("shots", [])
                    ))
                )
                if job.status == "error" or (job.status == "done" and not generation_running):
                    final_payload = {
                        "status": job.status,
                        "error_message": job.error_message,
                        "result": result,
                    }
                    yield f"event: final\ndata: {json.dumps(final_payload)}\n\n"
                    return
            finally:
                db.close()
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
