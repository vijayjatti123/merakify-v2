import asyncio
import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ConfigDict

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
from app.services.audio_video_service import AudioVideoModel

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

RETRY_CONTEXT_MARKER = "\n\n--- Retry context ---\n"


def _run_final_assembly(job_id, token, plan):
    from app.services import final_assembly_service
    with SessionLocal() as db:
        final_assembly_service.run(db, job_id, token, plan)


@router.post("/{job_id}/assemble", status_code=202)
def assemble_final_video(job_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    from app.services import final_assembly_service
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        plan = final_assembly_service.prepare(job, job_service.job_result(job) or {})
        token = job_service.claim_final_assembly(db, job_id, plan)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    background_tasks.add_task(_run_final_assembly, job_id, token, plan)
    return {"job_id": job_id, "assembly_token": token, "status": "running"}


class FaceEnhanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_video_key: str = Field(min_length=1, max_length=1024)


@router.post("/{job_id}/shots/{shot_number}/enhance-face", status_code=202)
def enhance_face(job_id: str, shot_number: int, payload: FaceEnhanceRequest, db: Session = Depends(get_db)):
    from app.config import settings
    if not settings.replicate_api_token:
        raise HTTPException(503, "Face enhancement is not configured.")
    try:
        return job_service.claim_face_enhancement(db, job_id, shot_number, payload.expected_video_key)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


class VideoGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_model: AudioVideoModel | None = None


class PreviewReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_key: str = Field(default="", max_length=1024)
    hint: str = Field(default="", max_length=1000)


class PreviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=64)
    accept: bool
    acknowledge: bool = False


@router.post("/{job_id}/shots/{shot_number}/preview/replacement", status_code=202)
def regenerate_preview_image(job_id: str, shot_number: int, payload: PreviewReplacementRequest,
                             background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    from app.services import preview_replacement as replacement
    try:
        token, snapshot = replacement.claim(db, job_id, shot_number, payload.expected_key, "generated")
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    background_tasks.add_task(replacement.run, job_id, shot_number, token, snapshot, payload.hint)
    return {"status": "working", "token": token}


@router.post("/{job_id}/shots/{shot_number}/preview/upload", status_code=202)
async def upload_preview_image(job_id: str, shot_number: int, background_tasks: BackgroundTasks,
                               file: UploadFile = File(...), expected_key: str = Form(""),
                               fit: str = Form("crop"), db: Session = Depends(get_db)):
    from app.services import preview_replacement as replacement
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        data = await file.read(replacement.MAX_BYTES + 1)
        data = replacement.normalize_upload(data, job.aspect_ratio, fit)
        token, snapshot = replacement.claim(db, job_id, shot_number, expected_key, "uploaded")
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    finally:
        await file.close()
    background_tasks.add_task(replacement.run, job_id, shot_number, token, snapshot, upload=data)
    return {"status": "working", "token": token}


@router.post("/{job_id}/shots/{shot_number}/preview/decision")
def decide_preview_image(job_id: str, shot_number: int, payload: PreviewDecisionRequest,
                         db: Session = Depends(get_db)):
    from app.services import preview_replacement as replacement
    try:
        replacement.decide(db, job_id, shot_number, payload.token, payload.accept, payload.acknowledge)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"status": "accepted" if payload.accept else "discarded"}


class VideoRegenerateRequest(VideoGenerateRequest):
    model_config = ConfigDict(extra="forbid")
    hint: str = Field(default="", max_length=1000)
    expected_attempt: str = Field(min_length=1, max_length=160)


@router.post("/{job_id}/shots/{shot_number}/video/regenerate", status_code=202)
def regenerate_video(job_id: str, shot_number: int, payload: VideoRegenerateRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    from app.services import video_generation_service as video
    try:
        _, shot = job_service.video_source(db, job_id, shot_number)
        # A previous clip is never a valid source for a new clip. If the
        # accepted opening image is missing, recover that image first even when
        # an older video remains available for comparison.
        if not shot.get("still_frame_url"):
            token = job_service.claim_still_retry(db, job_id, shot_number, payload.expected_attempt)
            background_tasks.add_task(_recover_missing_still, job_id, shot_number, token, payload.expected_attempt, payload.hint, True, payload.audio_model)
            return {"shot_number": shot_number, "status": "generating_still"}
        return video.start(db, job_id, shot_number, regenerate=True, hint=payload.hint,
                           expected_attempt=payload.expected_attempt, audio_model=payload.audio_model)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        raise HTTPException(502, "Video regeneration failed or is uncertain; inspect shot status before retrying") from error


def _recover_missing_still(job_id, number, token, expected_attempt, hint, generate_video=True, audio_model=None):
    from app.services import still_frame_service, video_generation_service
    with SessionLocal() as db:
        result = None
        events = []
        try:
            result, shot = job_service.video_source(db, job_id, number)
            if shot.get("still_retry_token") != token:
                return
            emit = lambda agent, message: events.append((agent, message))
            still_frame_service.generate_still_frames(result, job_id=job_id, emit=emit, shot_numbers={number},
                feedback_by_shot={number: hint} if hint.strip() else None)
            if not job_service.finish_still_retry(db, job_id, number, token, result):
                return
            if (shot.get("still_frame_url") and generate_video
                    and shot.get("still_frame_verification", {}).get("approved") is True):
                # Reuse Module S/R, including existing dialogue audio. No replanning/TTS.
                video_generation_service.start(db, job_id, number, regenerate=True,
                    expected_attempt=expected_attempt, hint=hint, audio_model=audio_model)
        except Exception as error:
            db.rollback()
            if result is not None and not shot.get("still_frame_url"):
                shot.update(still_frame_status="failed", still_frame_warning="This shot couldn't be generated — try regenerating it.")
                job_service.finish_still_retry(db, job_id, number, token, result)
            events.append(("still_frame", "WARNING: Shot " + str(number)
                + ": regeneration did not complete. Check the shot's current output before retrying. " + type(error).__name__))
        finally:
            job_service.append_events(db, job_id, events)


@router.post("/{job_id}/shots/{shot_number}/preview/retry", status_code=202)
def retry_preview(job_id: str, shot_number: int, payload: VideoRegenerateRequest,
                  background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Recover an image only. Video generation remains a separate paid action."""
    try:
        result, _ = job_service.video_source(db, job_id, shot_number)
        if result.get("audio_assembly_pending"):
            raise ValueError("Preview preparation is still running; please wait")
        token = job_service.claim_still_retry(db, job_id, shot_number, payload.expected_attempt)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    background_tasks.add_task(_recover_missing_still, job_id, shot_number, token,
                              payload.expected_attempt, "", False)
    return {"status": "generating", "shot_number": shot_number}


@router.get("/{job_id}/shots/{shot_number}/video-request")
def preview_video_request(job_id: str, shot_number: int, audio_model: AudioVideoModel | None = None, db: Session = Depends(get_db)):
    from app.services import video_generation_service as video
    try:
        result, shot = job_service.video_source(db, job_id, shot_number)
        return video.translate(result, shot, audio_model=audio_model)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/{job_id}/shots/{shot_number}/video", status_code=202)
def generate_video(job_id: str, shot_number: int, payload: VideoGenerateRequest | None = None, db: Session = Depends(get_db)):
    from app.services import video_generation_service as video
    try:
        return video.start(db, job_id, shot_number, audio_model=payload.audio_model if payload else None)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except Exception as error:
        raise HTTPException(502, "Video submission failed or is uncertain. Check the shot status before any further action.") from error


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
        ad_type=job.ad_type,
        ad_brief=json.loads(job.ad_brief_json or "{}"),
        aspect_ratio=job.aspect_ratio,
        visual_style=job.visual_style,
        color_grade=job.color_grade,
        quality=job.quality,
        language=job.language,
        ai_model=job.ai_model,
        video_model=job.video_model,
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
    video_model: str | None = None,
    script_text: str | None = None,
    resolutions: dict | None = None,
    product_ids: list[str] | None = None,
    creative_direction: dict | None = None,
    ad_type: str = "character",
    ad_brief: dict | None = None,
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
        video_model=video_model,
        script_text=script_text,
        resolutions=resolutions,
        product_ids=product_ids,
        creative_direction=creative_direction,
        ad_type=ad_type, ad_brief=ad_brief,
    )
    job_service.queue_pipeline_task(db, job.id, "plan")
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


@router.get("")
def list_jobs(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    return job_service.list_job_summaries(db, limit, offset)


@router.post("/{job_id}/retry-failed", response_model=JobOut)
def retry_failed_job(job_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Retry failed planning with the same inputs, not a contrasting creative premise."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "error":
        raise HTTPException(409, "Only a failed job can be retried here")
    retried = job_service.copy_job_for_retry(db, job)
    job_service.queue_pipeline_task(db, retried.id, "plan")
    return _job_out(retried)


def _resolve_brief_mentions(payload: JobCreate, db: Session):
    """Translate selected mention IDs into the existing explicit resolution map.

    Never infer a vault identity from a typed name or trust client descriptions.
    No pipeline/schema of stored resolutions changes are needed.
    """
    from app.services import character_service
    brief = payload.brief.strip()
    script = payload.script_text.strip() if payload.script_text is not None else None
    resolutions = payload.resolutions.model_dump() if payload.resolutions else None
    if not payload.character_mentions:
        return brief, script, resolutions
    resolutions = resolutions or {"characters": {}, "locations": {}}
    replacements = {}
    for token, character_id in payload.character_mentions.items():
        if not re.fullmatch(r"@[\w-]{1,120}", token):
            raise HTTPException(422, "Invalid character mention. Please select the character again.")
        pattern = r"(?<![\w@])" + re.escape(token) + r"(?![\w-])"
        if not re.search(pattern, brief):
            raise HTTPException(422, "A selected character is no longer mentioned. Please select it again.")
        character = character_service.get_character(db, character_id)
        if character is None or not character_service.is_customer_selectable(character):
            raise HTTPException(422, "A selected character is no longer available. Please choose another.")
        resolution = {"mode": "vault", "character_id": character.id}
        for name, existing in resolutions["characters"].items():
            if name.strip().casefold() == character.display_name.strip().casefold() and existing != resolution:
                raise HTTPException(422, "Two selections use the same character name. Please choose one.")
        resolutions["characters"][character.display_name] = resolution
        replacements[token] = character.display_name
    # One replacement pass avoids changing text introduced by another replacement.
    pattern = r"(?<![\w@])(" + "|".join(re.escape(t) for t in sorted(replacements, key=len, reverse=True)) + r")(?![\w-])"
    brief = re.sub(pattern, lambda m: replacements[m[0]], brief)
    if script is not None:
        script = re.sub(pattern, lambda m: replacements[m[0]], script)
    return brief, script, resolutions


@router.post("", response_model=JobOut)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not payload.brief.strip():
        raise HTTPException(status_code=400, detail="brief cannot be empty")
    if not payload.language.strip():
        raise HTTPException(status_code=400, detail="language cannot be empty")
    from app.services.product_service import selected
    try:
        selected(db, payload.product_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from app.services.clarifier_service import accepted_direction
    try:
        direction = accepted_direction(db, payload)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    brief, script, resolutions = _resolve_brief_mentions(payload, db)
    return _create_and_start_job(
        brief,
        background_tasks,
        db,
        aspect_ratio=payload.aspect_ratio,
        visual_style=payload.visual_style if "visual_style" in payload.model_fields_set else None,
        color_grade=payload.color_grade if "color_grade" in payload.model_fields_set else None,
        quality=payload.quality,
        language=payload.language.strip(),
        ai_model=payload.ai_model,
        video_model=payload.video_model,
        script_text=script,
        resolutions=resolutions,
        product_ids=payload.product_ids,
        creative_direction=direction,
        ad_type=payload.ad_type, ad_brief=payload.ad_brief.model_dump(exclude_defaults=True),
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

    original_json = job.result_json
    revision = result.get("plan_revision", 0)
    if (result.get("generation_approved") or revision) and payload.expected_plan_revision != revision:
        raise HTTPException(status_code=409, detail="Your plan changed. Refresh before editing it.")
    if payload.expected_plan_revision is not None and payload.expected_plan_revision != revision:
        raise HTTPException(status_code=409, detail="Your plan changed. Refresh before editing it.")

    edits_by_number = {edit.shot_number: edit for edit in payload.shots}
    if len(edits_by_number) != len(payload.shots):
        raise HTTPException(status_code=400, detail="shot_number values must be unique")

    stored_numbers = {shot["shot_number"] for shot in result["shots"]}
    unknown_numbers = set(edits_by_number) - stored_numbers
    if unknown_numbers:
        raise HTTPException(status_code=400, detail="edited shot_number was not found in this job")

    changed_numbers = set()
    merged_shots = []
    for shot in result["shots"]:
        merged = dict(shot)
        edit = edits_by_number.get(shot["shot_number"])
        if edit:
            fields = edit.model_dump(exclude_unset=True, exclude_none=True)
            if any(shot.get(k) != v for k, v in fields.items()):
                changed_numbers.add(shot["shot_number"])
            merged.update(fields)
            # All direction fields are shown together for explicit human review.
            # Invalidate the previous approval stamp, never silently rewrite them.
            merged.pop("direction_source", None)
            if edit.speech_mode is not None:
                merged["has_dialogue"] = edit.speech_mode != "none"
        merged_shots.append(merged)

    if not changed_numbers:
        return _job_out(job, result)
    continuity = result["continuity"]
    target_duration_sec = result["format"]["duration_target_sec"]
    validated = validate_and_correct(
        merged_shots,
        continuity["characters"],
        target_duration_sec,
        narrator_voice_ref=continuity.get("narrator_voice_ref"),
        source_script_text=result.get("source_script_text"),
        ad_direction_plan=result.get("ad_direction"),
        approved_story=result.get("script"),
        minimum_shot_seconds=result.get("planning_constraints", {}).get("minimum_shot_seconds"),
        semantic_review=False,
        review_shot_numbers=changed_numbers,
        emit=lambda key, note: job_service.append_event(db, job_id, key, note),
    )
    validated_shots = _attach_voice_refs(
        validated["shots"], continuity["characters"], continuity.get("narrator_voice_ref")
    )
    originals = {shot["shot_number"]: shot for shot in result["shots"]}
    validated_shots = [shot if shot["shot_number"] in changed_numbers else originals[shot["shot_number"]]
                       for shot in validated_shots]

    updated_result = dict(result)
    updated_result.update(
        shots=validated_shots,
        generation_approved=False,
        qa=validated["qa"],
        assembly=validated["assembly"],
    )
    try:
        updated_result = job_service.save_plan_revision(db, job_id, original_json, updated_result, changed_numbers)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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

    if result.get("generation_approved"):
        return _job_out(job, result)
    from app.services.director_review import review
    technical = review(result.get("shots", []), result.get("continuity", {}).get("characters", []),
                       result.get("planning_constraints", {}).get("minimum_shot_seconds"),
                       {"ad_type": job.ad_type, "ad_brief": json.loads(job.ad_brief_json or "{}")},
                       shot_numbers=set(result.get("plan_edited_shots", [])) or None)
    if not technical["approved"]:
        raise HTTPException(status_code=422, detail="Correct the plan details before approval: " + "; ".join(
            f"Shot {i['shot_number']}: {i['problem']}" for i in technical["issues"]))
    updated_result = dict(result)
    updated_result["qa"] = technical
    updated_result["generation_approved"] = True
    updated_result["preview_preparation_pending"] = True
    updated_result["audio_assembly_pending"] = any(shot.get("has_dialogue") for shot in result.get("shots", []))
    edited = set(result.get("plan_edited_shots", []))
    updated_result["shots"] = [
        {
            **shot,
            "status": SHOT_STATUS_PENDING,
            "error_message": None,
            "dialogue_audio_url": None,
            "dialogue_audio_provider": None,
        } if not edited or shot["shot_number"] in edited else shot
        for shot in result.get("shots", [])
    ]
    # Persist the exact visual checklist before any image provider is called.
    # Generation and verification consume this same contract.
    from app.services.preview_plan import attach_visual_contracts
    attach_visual_contracts(updated_result, edited or None)
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
    job_service.queue_pipeline_task(db, job_id, "prepare")
    db.refresh(job)
    return _job_out(job, updated_result)


def _retry_preview_preparation(job_id):
    from app.agents.director import finalize_audio_assembly
    with SessionLocal() as db:
        finalize_audio_assembly(db, job_id)


@router.post("/{job_id}/previews/retry", response_model=JobOut)
def retry_preview_preparation(job_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Resume after post-audio preparation failed, keeping the plan and real audio."""
    try:
        job_service.claim_preview_preparation(db, job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    job_service.queue_pipeline_task(db, job_id, "resume")
    job = job_service.get_job(db, job_id)
    return _job_out(job, job_service.job_result(job))


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
            from app.services.camera_direction import for_shot as camera_for_shot
            camera = camera_for_shot(candidate)
            # Copy only visual fields: dialogue, timing, identity and all neighbors remain authoritative.
            proposed = [
                {**shot, **{key: candidate[key] for key in visual_fields}, "camera_direction": camera} if shot["shot_number"] == shot_number else dict(shot)
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
    from app.services.product_service import job_references
    product_ids = [p["product_id"] for p in job_references(db, job.id)]
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
        video_model=job.video_model,
        product_ids=product_ids,
        ad_type=job.ad_type, ad_brief=json.loads(job.ad_brief_json or "{}"),
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
                    and (result.get("audio_assembly_pending") or result.get("preview_preparation_pending") or any(
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
