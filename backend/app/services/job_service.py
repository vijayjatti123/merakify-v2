import json
from datetime import datetime
from typing import Any, Optional, get_args

from sqlalchemy.orm import Session

from app.models import AgentEvent, Job, VideoTask, FinalAssembly, FaceEnhancement, ProviderSubmissionGate
from app.schemas import ColorGrade, VisualStyle, style_from_brief


def create_clarifier_session(db, raw_brief, known_fields, context=None):
    from app.models import ClarifierSession
    row = ClarifierSession(raw_brief=raw_brief, known_fields=known_fields,
        gathered={"_context": context} if context else {})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_clarifier_session(db, session_id):
    from app.models import ClarifierSession
    return db.get(ClarifierSession, session_id, populate_existing=True)


def update_clarifier_session(db, session_id, revision, changes):
    """Atomic compare-and-swap: a second writer cannot overwrite a newer turn."""
    from app.models import ClarifierSession
    count = db.query(ClarifierSession).filter(
        ClarifierSession.session_id == session_id,
        ClarifierSession.revision == revision,
    ).update({**changes, "revision": revision + 1, "updated_at": datetime.utcnow()},
             synchronize_session=False)
    if count != 1:
        db.rollback()
        raise ValueError("Session changed; reload it before retrying.")
    db.commit()
    db.expire_all()
    return get_clarifier_session(db, session_id)


def create_job(
    db: Session,
    brief: str,
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
) -> Job:
    from app.video_models import validate_selection
    validate_selection(video_model, ai_model, language, quality)
    visual_style = visual_style if visual_style is not None else style_from_brief(brief, "visual_style")
    color_grade = color_grade if color_grade is not None else style_from_brief(brief, "color_grade")
    if visual_style not in get_args(VisualStyle) or color_grade not in get_args(ColorGrade):
        raise ValueError("Invalid job style settings")
    # Stored fields are authoritative. Keep a canonical prose mirror for Module H
    # and existing brief consumers, including callers that only send typed fields.
    for label, values, selected in (
        ("Visual style", get_args(VisualStyle), visual_style),
        ("Whole-video color grade", get_args(ColorGrade), color_grade),
    ):
        metadata_lines = {f"{label}: {value}." for value in values}
        brief = "\n".join(line for line in brief.split("\n") if line.strip() not in metadata_lines).rstrip()
        if selected != values[0]:
            brief += f"\n\n{label}: {selected}."
    from app.services.product_service import selected, JobProduct
    products = selected(db, product_ids or [])
    if products:
        brief += "\n\nApproved product references: " + ", ".join(p.name for p in products) + ". Preserve the named products and their packaging; no voice or character identity is attached to them."
    job = Job(
        brief=brief,
        aspect_ratio=aspect_ratio,
        visual_style=visual_style,
        color_grade=color_grade,
        quality=quality,
        language=language,
        ai_model=ai_model,
        video_model=video_model,
        script_text=script_text,
        resolutions_json=json.dumps(resolutions, ensure_ascii=False) if resolutions is not None else None,
        creative_direction_json=json.dumps(creative_direction, ensure_ascii=False) if creative_direction else None,
        status="queued",
    )
    db.add(job)
    db.flush()
    for product in products:
        db.add(JobProduct(job_id=job.id, product_id=product.id, name=product.name, object_key=product.accepted_key))
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str) -> Optional[Job]:
    return db.query(Job).filter(Job.id == job_id).first()


def list_job_summaries(db: Session, limit: int, offset: int) -> dict:
    rows = (db.query(Job.id, Job.brief, Job.status, Job.created_at)
            .order_by(Job.created_at.desc(), Job.id.desc()).offset(offset).limit(limit + 1).all())
    return {
        "jobs": [{"id": row.id, "brief": row.brief.split("\n\n")[0][:200],
                  "status": row.status, "created_at": row.created_at} for row in rows[:limit]],
        "has_more": len(rows) > limit,
    }


def copy_job_for_retry(db: Session, source: Job) -> Job:
    # Already-validated intake must survive recovery byte-for-byte, including
    # source script and vault resolutions. Do not re-fold style prose.
    job = Job(**{field: getattr(source, field) for field in (
        "brief", "aspect_ratio", "visual_style", "color_grade", "quality",
        "language", "ai_model", "video_model", "script_text", "resolutions_json", "creative_direction_json",
    )}, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    from app.models import AgentCheckpoint
    for row in db.query(AgentCheckpoint).filter_by(job_id=source.id).all():
        db.add(AgentCheckpoint(job_id=job.id, input_hash=row.input_hash, response_json=row.response_json))
    db.commit()
    return job


def get_agent_checkpoint(db, job_id, input_hash):
    from app.models import AgentCheckpoint
    row = db.get(AgentCheckpoint, (job_id, input_hash))
    return json.loads(row.response_json) if row else None


def queue_pipeline_task(db, job_id, kind):
    from app.models import PipelineTask, new_id
    from sqlalchemy.exc import IntegrityError
    if kind not in {"plan", "prepare", "resume"}:
        raise ValueError("Unknown pipeline operation")
    row = db.get(PipelineTask, job_id, populate_existing=True)
    completed_plan = (row and row.kind == "plan" and row.status == "running" and kind == "prepare"
                      and get_job(db, job_id).status == "done")
    if row and row.status in {"queued", "running"} and not completed_plan:
        raise ValueError("This operation is already running")
    values = dict(kind=kind, token=new_id(), status="queued", heartbeat_at=datetime.utcnow())
    if row:
        changed = db.query(PipelineTask).filter_by(job_id=job_id, token=row.token, status=row.status).update(
            values, synchronize_session=False)
        if changed != 1:
            db.rollback()
            raise ValueError("This operation is already running")
    else:
        db.add(PipelineTask(job_id=job_id, **values))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("This operation is already running") from None


def fail_pipeline_preparation(db, job_id, message):
    """Release visible busy states without discarding accepted media."""
    job = get_job(db, job_id)
    if not job:
        return
    result = json.loads(job.result_json or "null")
    if result:
        for field in ("audio_assembly_pending", "preview_preparation_pending", "video_prompts_pending"):
            result[field] = False
        result["video_prompt_error"] = message
        for shot in result.get("shots", []):
            if shot.get("still_frame_status") in {"pending", "generating"} and not shot.get("still_frame_url"):
                shot.update(still_frame_status="failed", still_frame_warning=message)
        job.result_json = json.dumps(result)
    job.status = "error"
    job.error_message = message
    db.add(AgentEvent(job_id=job_id, agent_key="error", note=message))
    db.commit()


def claim_pipeline_task(db):
    from app.models import PipelineTask
    row = db.query(PipelineTask).filter_by(status="queued").order_by(PipelineTask.heartbeat_at).first()
    if not row:
        return None
    identity = (row.job_id, row.token, row.kind)
    changed = db.query(PipelineTask).filter_by(job_id=row.job_id, token=row.token, status="queued").update(
        {PipelineTask.status: "running", PipelineTask.heartbeat_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return identity if changed else None


def heartbeat_pipeline_task(db, job_id, token, status="running"):
    from app.models import PipelineTask
    db.query(PipelineTask).filter_by(job_id=job_id, token=token, status="running").update(
        {PipelineTask.status: status, PipelineTask.heartbeat_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()


def pause_expired_pipeline_tasks(db):
    from datetime import timedelta
    from app.models import PipelineTask
    cutoff = datetime.utcnow() - timedelta(seconds=90)
    for row in db.query(PipelineTask).filter(PipelineTask.status == "running", PipelineTask.heartbeat_at < cutoff).all():
        changed = db.query(PipelineTask).filter(PipelineTask.job_id == row.job_id,
            PipelineTask.token == row.token, PipelineTask.status == "running", PipelineTask.heartbeat_at < cutoff).update(
            {PipelineTask.status: "paused"}, synchronize_session=False)
        if not changed:
            continue
        job = get_job(db, row.job_id)
        result = json.loads(job.result_json or "null")
        if result:
            result["audio_assembly_pending"] = False
            result["preview_preparation_pending"] = False
            result["video_prompts_pending"] = False
            result["video_prompt_error"] = "Preparation was interrupted. Your completed work is saved; retry preparation."
            for shot in result.get("shots", []):
                if shot.get("still_frame_status") == "generating":
                    shot.update(still_frame_status="failed", still_frame_warning="Preparation was interrupted; retry this preview.")
            job.result_json = json.dumps(result)
        job.status = "error"
        job.error_message = "Preparation was interrupted. Your completed work is saved; please retry."
        db.add(AgentEvent(job_id=row.job_id, agent_key="error", note=job.error_message))
    db.commit()


def save_agent_checkpoint(db, job_id, input_hash, response):
    from app.models import AgentCheckpoint
    db.merge(AgentCheckpoint(job_id=job_id, input_hash=input_hash, response_json=json.dumps(response)))
    db.commit()


def set_status(db: Session, job_id: str, status: str, error_message: Optional[str] = None) -> None:
    job = get_job(db, job_id)
    if not job:
        return
    job.status = status
    if error_message is not None:
        job.error_message = error_message
    db.commit()


def set_result(db: Session, job_id: str, result: dict) -> None:
    job = get_job(db, job_id)
    if not job:
        return
    from app.services.still_frame_service import invalidate_changed_stills
    invalidate_changed_stills(result)
    job.result_json = json.dumps(result)
    db.commit()


def append_event(db: Session, job_id: str, agent_key: str, note: str) -> AgentEvent:
    event = AgentEvent(job_id=job_id, agent_key=agent_key, note=note)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def append_shot_status_event(
    db: Session,
    job_id: str,
    shot_number: int,
    status: str,
    *,
    message: str,
    **fields,
) -> AgentEvent:
    payload = {"shot_number": shot_number, "status": status, "message": message, **fields}
    return append_event(db, job_id, "shot_status", json.dumps(payload))


def update_shot_fields(db: Session, job_id: str, shot_number: int, **fields) -> dict:
    """Update one shot inside the existing persisted job result."""
    job = get_job(db, job_id)
    if not job:
        raise LookupError("job not found")
    result = job_result(job)
    if not result:
        raise ValueError("job has no stored result")
    for shot in result.get("shots", []):
        if shot.get("shot_number") == shot_number:
            shot.update(fields)
            set_result(db, job_id, result)
            return shot
    raise LookupError("shot not found")


def get_events_since(db: Session, job_id: str, after_id: Optional[str] = None) -> list[AgentEvent]:
    q = db.query(AgentEvent).filter(AgentEvent.job_id == job_id).order_by(AgentEvent.created_at)
    events = q.all()
    if not after_id:
        return events
    idx = next((i for i, e in enumerate(events) if e.id == after_id), -1)
    return events[idx + 1 :]


def job_result(job: Job) -> Optional[Any]:
    result = json.loads(job.result_json) if job.result_json else None
    if result:
        result["video_model"] = job.video_model
        from app.services import storage_service
        from app.services.still_frame_service import invalidate_changed_stills
        invalidate_changed_stills(result)
        for shot in result.get("shots", []):
            replacement = shot.get("preview_replacement")
            if replacement:
                from app.services.preview_replacement import expired
                if replacement.get("status") == "working" and expired(replacement):
                    replacement.update(status="failed", warning="Image replacement timed out. Your current image is unchanged; try again.")
                if replacement.get("key"):
                    try:
                        replacement["url"] = storage_service.asset_url(replacement["key"])
                    except Exception:
                        replacement["url"] = None
            # Initial batch generation has no manual-retry lease. Absence of that
            # lease must not turn a live preview into a false timeout on read.
            if shot.get("still_frame_status") == "generating" and shot.get("still_retry_token") and still_retry_expired(shot):
                shot["still_frame_status"] = "failed"
                shot["still_frame_warning"] = "Shot regeneration timed out. Please try regenerating it again."
            elif not shot.get("still_frame_status") and shot.get("still_frame_warning") and not shot.get("still_frame_key"):
                shot["still_frame_status"] = "failed"
            if shot.get("still_frame_key"):
                try:
                    shot["still_frame_url"] = storage_service.asset_url(shot["still_frame_key"])
                    display = shot.get("still_frame_display") or {}
                    if display.get("source_key") != shot["still_frame_key"]:
                        shot.pop("still_frame_display", None)
                    else:
                        try:
                            for variant in display.get("variants", []):
                                variant["url"] = storage_service.asset_url(variant["key"])
                        except Exception:
                            shot.pop("still_frame_display", None)
                except Exception:
                    # Reading a job must remain possible during storage outages.
                    shot["still_frame_url"] = None
                    shot["still_frame_warning"] = "Still frame temporarily unavailable: could not refresh image access."
        # Video attempts live separately: replanning must never lose a paid task.
        from sqlalchemy.orm import object_session
        db = object_session(job) if isinstance(job, Job) else None
        if db:
            videos = {int(v.shot_number): json.loads(v.data_json) for v in db.query(VideoTask).filter(VideoTask.job_id == job.id)}
            for shot in result.get("shots", []):
                fields = videos.get(shot.get("shot_number"))
                if fields:
                    # Revised plan JSON can contain a previous task's presentation
                    # fields. Only the current durable attempt may supply them.
                    for key in [key for key in shot if key.startswith("video_")]:
                        shot.pop(key)
                    shot.update(fields)
                    if fields.get("video_key"):
                        try:
                            shot["video_url"] = storage_service.asset_url(fields["video_key"])
                        except Exception:
                            shot["video_url"] = None
                            shot["video_error"] = "Stored video temporarily inaccessible; refresh later."
                    from app.services.video_generation_service import source_fingerprint
                    shot["video_source_changed"] = fields.get("video_source_hash") != source_fingerprint(shot)
        if db:
            enhancements = {v.shot_number: v for v in db.query(FaceEnhancement).filter_by(job_id=job.id).populate_existing()}
            for shot in result.get("shots", []):
                enhancement = enhancements.get(shot.get("shot_number"))
                if enhancement:
                    data = json.loads(enhancement.data_json)
                    shot["face_enhancement"] = {k: data.get(k) for k in ("status", "completed_frames", "total_frames", "warning")}
        if db:
            row = db.query(FinalAssembly).filter_by(job_id=job.id).populate_existing().one_or_none()
            if row:
                data = json.loads(row.data_json)
                from app.services.final_assembly_service import fingerprint
                data["stale"] = data.get("source_hash") != fingerprint(result, job.aspect_ratio, job.quality, job.color_grade)
                if data.get("key"):
                    try:
                        data["url"] = storage_service.asset_url(data["key"])
                    except Exception:
                        data["url"] = None
                        data["error"] = "Final video temporarily inaccessible; refresh later."
                if data.get("status") == "running" and final_assembly_expired(data):
                    data["status"] = "failed"
                    data["error"] = "Assembly worker did not finish within 30 minutes. You can assemble again."
                result["final_video"] = data
        result["shots_needing_attention"] = [s["shot_number"] for s in result.get("shots", [])
            if s.get("still_frame_status") == "failed" and not s.get("still_frame_url") and not s.get("video_url")]
    return result


def mutate_preview_replacement(db, job_id, number, mutate):
    """CAS the latest plan; a background candidate cannot overwrite sibling edits."""
    for _ in range(4):
        db.expire_all()
        job = get_job(db, job_id)
        if not job:
            raise LookupError("Job not found")
        old = job.result_json
        result = json.loads(old or "{}")
        shot = next((s for s in result.get("shots", []) if s.get("shot_number") == number), None)
        if shot is None:
            raise LookupError("Shot not found")
        value = mutate(job, result, shot)
        count = db.query(Job).filter(Job.id == job_id, Job.result_json == old).update(
            {Job.result_json: json.dumps(result)}, synchronize_session=False)
        if count == 1:
            db.commit()
            db.expire_all()
            return value
        db.rollback()
    raise ValueError("Your plan changed. Refresh before trying again.")


def video_source(db, job_id, number):
    job = get_job(db, job_id)
    if not job:
        raise LookupError("Job not found")
    result = job_result(job) or {}
    if job.status != "done" or result.get("audio_assembly_pending") or result.get("assembly", {}).get("provisional"):
        raise ValueError("Finish final shot planning and audio approval first")
    result.update(ai_model=job.ai_model, quality=job.quality, aspect_ratio=job.aspect_ratio, language=job.language, video_model=job.video_model)
    shot = next((s for s in result.get("shots", []) if s.get("shot_number") == number), None)
    if shot is None:
        raise LookupError("Shot not found")
    # Older jobs predate reference-sheet propagation. Resolve by saved ID only;
    # never re-match display names or change their approved identity rendering.
    from app.models import Character
    from app.services import product_service
    for character in result.get("continuity", {}).get("characters", []):
        if character.get("character_id") and not character.get("style_variant_id") and job.visual_style == "Natural":
            row = db.get(Character, character["character_id"])
            if row and row.status == "approved" and row.reference_sheet_url:
                character.setdefault("reference_sheet_url", row.reference_sheet_url)
    shot["approved_product_references"] = product_service.job_references(db, job_id)
    return result, shot


def still_retry_expired(shot):
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(shot["still_retry_started_at"])).total_seconds() > 900
    except (KeyError, ValueError, TypeError):
        return True


def claim_preview_preparation(db, job_id):
    job = get_job(db, job_id)
    if not job:
        raise LookupError("Job not found")
    old = job.result_json
    result = json.loads(old or "{}")
    shots = result.get("shots", [])
    if not result.get("generation_approved") or not shots:
        raise ValueError("Create previews from the completed plan first")
    if result.get("audio_assembly_pending") or result.get("preview_preparation_pending") or any(s.get("still_frame_status") == "generating" for s in shots):
        raise ValueError("Preview preparation is already running")
    if any(s.get("video_url") for s in shots) or (any(s.get("still_frame_url") for s in shots) and not result.get("video_prompt_error")):
        raise ValueError("Use the individual shot's Retry preview action to preserve existing output")
    if any(s.get("has_dialogue") and (s.get("status") != "done" or not s.get("dialogue_audio_url")) for s in shots):
        raise ValueError("Speech preparation must finish before previews can be retried")
    if job.status != "error" and not result.get("assembly", {}).get("provisional"):
        raise ValueError("No failed preview preparation to retry")
    result["audio_assembly_pending"] = True
    result["preview_preparation_pending"] = True
    changed = db.query(Job).filter(Job.id == job_id, Job.result_json == old, Job.status == job.status).update(
        {Job.result_json: json.dumps(result), Job.status: "done", Job.error_message: None}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise ValueError("Job changed; refresh before retrying")
    db.commit()
    db.expire_all()


def claim_still_retry(db, job_id, number, expected_attempt):
    """Claim only a missing-output shot; CAS also guards SQLite double clicks."""
    import uuid
    from datetime import datetime, timezone
    result, shot = video_source(db, job_id, number)
    if not result.get("generation_approved") or not (shot.get("compiled_prompt") or shot.get("preview_input")):
        raise ValueError("Approve the completed shot plan before regenerating")
    if shot.get("still_frame_url") or shot.get("video_url"):
        raise ValueError("Shot output changed; refresh before regenerating")
    if shot.get("video_status") in {"submitting", "processing", "submission_unknown"}:
        raise ValueError("Video is busy or needs reconciliation; refresh first")
    if (shot.get("video_task_id") or shot.get("video_submitted_at") or "none") != expected_attempt:
        raise ValueError("Video attempt changed; refresh before regenerating")
    job = get_job(db, job_id)
    old = job.result_json
    stored = json.loads(old)
    target = next(s for s in stored["shots"] if s["shot_number"] == number)
    if target.get("still_frame_status") == "generating" and not still_retry_expired(target):
        raise ValueError("This shot is already regenerating")
    token = uuid.uuid4().hex
    target.update(still_frame_status="generating", still_retry_token=token,
                  still_retry_started_at=datetime.now(timezone.utc).isoformat())
    changed = db.query(Job).filter(Job.id == job_id, Job.result_json == old).update(
        {Job.result_json: json.dumps(stored)}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise ValueError("Job changed; refresh before regenerating")
    db.commit()
    db.expire_all()
    return token


def finish_still_retry(db, job_id, number, token, regenerated):
    from app.services.still_frame_service import shot_fingerprint
    from app.services.preview_plan import preview_input
    job = db.query(Job).filter_by(id=job_id).with_for_update().populate_existing().one()
    old = job.result_json
    result = json.loads(old)
    target = next(s for s in result["shots"] if s["shot_number"] == number)
    source = next(s for s in regenerated["shots"] if s["shot_number"] == number)
    if target.get("still_retry_token") != token or still_retry_expired(target):
        db.rollback()
        return False
    # Dependencies are resolved by the worker after an upstream preview recovers.
    # Compare the current creative plan separately, and verify every consumed
    # dependency still points to the same accepted image before adopting it.
    current = dict(target)
    facts = preview_input(result, target)
    if facts:
        current['preview_input'] = facts
    dependencies = source.get('preview_dependencies', {})
    current['preview_dependencies'] = dependencies
    siblings = {str(s['shot_number']): s for s in result['shots']}
    dependencies_match = all(
        str(n) in siblings and
        (siblings[str(n)].get('still_frame_key') or siblings[str(n)].get('still_frame_url')) == key
        for n, key in dependencies.items())
    compatible = dependencies_match and shot_fingerprint(current) == shot_fingerprint(source)
    if not compatible:
        target.update(still_frame_status='failed', still_frame_error_kind='generation',
                      still_frame_warning='The plan or a reference changed during preview preparation. Retry this preview.')
        target.pop('still_retry_token', None)
        target.pop('still_retry_started_at', None)
    else:
        for key in list(target):
            if key.startswith("still_frame_") or key.startswith("still_retry_"):
                target.pop(key)
        target.update({k: v for k, v in source.items() if k.startswith("still_frame_")})
        if source.get('preview_input'):
            target['preview_input'] = source['preview_input']
        target['preview_dependencies'] = dependencies
        for entity, reference in regenerated.get("entity_references", {}).items():
            if reference.get("shot_number") == number:
                result.setdefault("entity_references", {})[entity] = reference
    changed = db.query(Job).filter(Job.id == job_id, Job.result_json == old).update(
        {Job.result_json: json.dumps(result)}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return False
    db.commit()
    db.expire_all()
    return compatible


def claim_video(db, job_id, number, fields, *, replace_token=None):
    # One current task per shot; retain old attempts in job-scoped history.
    # Compare-and-swap protects against double clicks, stale tabs and SQLite's
    # lack of SELECT FOR UPDATE. Uncertain submissions require reconciliation.
    if replace_token is not None:
        row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).one_or_none()
        if row is None:
            if replace_token != "none":
                raise ValueError("Video attempt changed; refresh before regenerating")
        else:
            old_json = row.data_json
            old = json.loads(old_json)
            token = old.get("video_task_id") or old.get("video_submitted_at")
            if token != replace_token or row.status not in {"done", "failed", "review_required"}:
                raise ValueError("Video attempt changed, is busy, or needs reconciliation; refresh before regenerating")
            history = old.pop("video_attempt_history", [])
            fields = {**fields, "video_url": None, "video_key": None,
                      "video_attempt_history": history + [old]}
            changed = db.query(VideoTask).filter(VideoTask.id == row.id, VideoTask.data_json == old_json).update(
                {VideoTask.data_json: json.dumps(fields), VideoTask.status: "submitting"}, synchronize_session=False)
            if changed != 1:
                db.rollback()
                raise ValueError("Another request already regenerated this shot")
            db.commit()
            db.expire_all()
            return

    from sqlalchemy.exc import IntegrityError
    row = VideoTask(job_id=job_id, shot_number=number, status="submitting", data_json=json.dumps(fields))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("This shot already has a video attempt; paid resubmission is disabled") from None


def update_video(db, job_id, number, **fields):
    expected_task = fields.pop("expected_task_id", None)
    expected_started = fields.pop("expected_submitted_at", None)
    for _ in range(3):
        row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).with_for_update().populate_existing().one()
        old_json = row.data_json
        data = json.loads(old_json)
        if ((expected_task is not None and data.get("video_task_id") != expected_task)
                or (expected_started is not None and data.get("video_submitted_at") != expected_started)):
            db.rollback()
            return False
        data.update(fields)
        changed = db.query(VideoTask).filter(VideoTask.id == row.id, VideoTask.data_json == old_json).update(
            {VideoTask.data_json: json.dumps(data), VideoTask.status: data["video_status"]}, synchronize_session=False)
        if changed == 1:
            db.commit()
            return True
        db.rollback()
    raise ValueError("Video task changed concurrently; retry the saved task update")



def pending_videos(db):
    return [(row.job_id, {**json.loads(row.data_json), "shot_number": int(row.shot_number)})
            for row in db.query(VideoTask).filter(VideoTask.status.in_(["processing", "submitting"])).all()]


def video_worker_lease(db, job_id, number, task, token, *, renew=False, release=False):
    """Short CAS lease on existing task JSON; no DB connection held during media work."""
    from datetime import timezone, timedelta
    row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).populate_existing().one_or_none()
    if row is None:
        return None
    old = row.data_json
    data = json.loads(old)
    lease = data.get("video_worker_lease", {})
    now = datetime.now(timezone.utc)
    if renew or release:
        if lease.get("token") != token:
            return None
    elif (row.status not in {"processing", "submitting"}
          or data.get("video_task_id") != task
          or (lease.get("until") and datetime.fromisoformat(lease["until"]) > now)):
        return None
    if release:
        data.pop("video_worker_lease", None)
    else:
        data["video_worker_lease"] = {"token": token, "until": (now + timedelta(seconds=120)).isoformat()}
    changed = db.query(VideoTask).filter(VideoTask.id == row.id, VideoTask.data_json == old).update(
        {VideoTask.data_json: json.dumps(data)}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    db.commit()
    return {**data, "shot_number": number}


def video_check_state(db, job_id, number, task, *, check=None, claim_retry=False):
    """CAS the existing task JSON: at most one paid compliance retry across workers/restarts."""
    row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).populate_existing().one()
    old_json = row.data_json
    data = json.loads(old_json)
    if data.get("video_task_id") != task or row.status != "processing":
        db.rollback()
        return None
    if check is None and not claim_retry:
        return data
    if claim_retry:
        if data.get("video_compliance_retries", 0):
            return None
        data.update(video_compliance_retries=1, video_status="submitting", video_retry_parent_task=task)
        from datetime import datetime, timezone
        data["video_submitted_at"] = datetime.now(timezone.utc).isoformat()
    else:
        data.setdefault("video_compliance_checks", {}).setdefault(task, check)
    changed = db.query(VideoTask).filter(VideoTask.id == row.id, VideoTask.data_json == old_json).update(
        {VideoTask.data_json: json.dumps(data), VideoTask.status: data["video_status"]}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    db.commit()
    db.expire_all()
    return data



def final_assembly_expired(data):
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - datetime.fromisoformat(data["started_at"])).total_seconds() > 1800


def claim_final_assembly(db, job_id, plan):
    from datetime import datetime, timezone
    from uuid import uuid4
    from sqlalchemy.exc import IntegrityError
    token = str(uuid4())
    data = {"token": token, "status": "running", "source_hash": plan["source_hash"],
            "started_at": datetime.now(timezone.utc).isoformat(), "plan": plan}
    row = db.query(FinalAssembly).filter_by(job_id=job_id).populate_existing().one_or_none()
    if row:
        old_json = row.data_json
        old = json.loads(old_json)
        if row.status == "running" and not final_assembly_expired(old):
            raise ValueError("Final assembly is already running for this job.")
        changed = db.query(FinalAssembly).filter(FinalAssembly.job_id == job_id, FinalAssembly.data_json == old_json).update(
            {FinalAssembly.status: "running", FinalAssembly.data_json: json.dumps(data)}, synchronize_session=False)
        if changed != 1:
            db.rollback()
            raise ValueError("Another request already started final assembly.")
    else:
        db.add(FinalAssembly(job_id=job_id, status="running", data_json=json.dumps(data)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Another request already started final assembly.") from None
    db.expire_all()
    return token


def finish_final_assembly(db, job_id, token, **fields):
    row = db.query(FinalAssembly).filter_by(job_id=job_id).with_for_update().populate_existing().one()
    data = json.loads(row.data_json)
    if data["token"] != token:
        db.rollback()
        return False
    data.update(fields)
    row.data_json = json.dumps(data)
    row.status = data["status"]
    db.commit()
    return True


# Module V persistence is separate from Module R/S's video attempt lifecycle.
def claim_face_enhancement(db, job_id, number, expected_key):
    import time
    from sqlalchemy.exc import IntegrityError
    _, shot = video_source(db, job_id, number)
    if not (shot.get("has_dialogue") and shot.get("video_provider") == "hedra"
            and shot.get("video_status") == "done" and shot.get("video_key")
            and shot.get("video_url") and not shot.get("video_source_changed")):
        raise ValueError("Enhance Face requires a current, completed Hedra dialogue video.")
    if shot["video_key"] != expected_key:
        raise ValueError("Video changed; refresh before enhancing.")
    if shot.get("video_face_enhanced"):
        raise ValueError("This video is already face-enhanced; repeated restoration is disabled.")
    video = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).populate_existing().one()
    from uuid import uuid4
    data = {"run_token": str(uuid4()), "status": "queued", "source_json": video.data_json, "source_key": expected_key,
            "completed_frames": 0, "total_frames": None}
    row = db.query(FaceEnhancement).filter_by(job_id=job_id, shot_number=number).populate_existing().one_or_none()
    if row:
        if row.status in {"queued", "running"}:
            raise ValueError("Face enhancement is already queued or running for this shot.")
        changed = db.query(FaceEnhancement).filter_by(id=row.id, data_json=row.data_json).update(
            {FaceEnhancement.status: "queued", FaceEnhancement.data_json: json.dumps(data), FaceEnhancement.heartbeat: time.time()}, synchronize_session=False)
        if changed != 1:
            db.rollback(); raise ValueError("Another request already started enhancement.")
    else:
        row = FaceEnhancement(job_id=job_id, shot_number=number, status="queued", data_json=json.dumps(data), heartbeat=time.time())
        db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback(); raise ValueError("Another request already started enhancement.") from None
    db.refresh(row)
    append_event(db, job_id, "face_enhancement", f"Shot {number}: face enhancement queued; current video remains available.")
    return {"status": "queued", "enhancement_id": row.id}


def next_face_enhancement(db):
    import time
    # Interrupted workers fail closed; never blindly resubmit an uncertain paid frame.
    for row in db.query(FaceEnhancement).filter(FaceEnhancement.status == "running", FaceEnhancement.heartbeat < time.time()-300).all():
        face_progress(db, row.id, status="failed", warning="Face enhancement worker stopped or timed out; original video preserved.")
        append_event(db, row.job_id, "face_enhancement", f"WARNING: Shot {row.shot_number}: enhancement interrupted; original video preserved.")
    row = db.query(FaceEnhancement).filter_by(status="queued").order_by(FaceEnhancement.heartbeat).first()
    if not row:
        return None
    data = json.loads(row.data_json); data["status"] = "running"
    changed = db.query(FaceEnhancement).filter_by(id=row.id, status="queued").update(
        {FaceEnhancement.status: "running", FaceEnhancement.data_json: json.dumps(data), FaceEnhancement.heartbeat: time.time()}, synchronize_session=False)
    db.commit()
    return row.id if changed == 1 else None


def face_progress(db, task_id, *, expected_run_token=None, **fields):
    import time
    row = db.query(FaceEnhancement).filter_by(id=task_id).populate_existing().one()
    if row.status not in {"running", "queued"}:
        raise ValueError("Enhancement attempt no longer active.")
    old = row.data_json; data = json.loads(old)
    if expected_run_token is not None and data.get("run_token") != expected_run_token:
        raise ValueError("Enhancement attempt was superseded.")
    data.update(fields)
    changed = db.query(FaceEnhancement).filter_by(id=task_id, data_json=old).update(
        {FaceEnhancement.data_json: json.dumps(data), FaceEnhancement.status: data["status"], FaceEnhancement.heartbeat: time.time()}, synchronize_session=False)
    if changed != 1:
        db.rollback(); raise ValueError("Enhancement attempt changed.")
    db.commit()


def finish_face_enhancement(db, task_id, stored, digest, *, expected_run_token=None):
    import time
    task = db.query(FaceEnhancement).filter_by(id=task_id).populate_existing().one()
    data = json.loads(task.data_json)
    if expected_run_token is not None and data.get("run_token") != expected_run_token:
        raise ValueError("Enhancement attempt was superseded.")
    _, shot = video_source(db, task.job_id, task.shot_number)
    if task.status != "running" or shot.get("video_source_changed"):
        raise ValueError("Shot changed during enhancement; enhanced result was not applied.")
    original = json.loads(data["source_json"])
    updated = {**original, "video_key": stored["key"], "video_url": stored["url"],
               "video_sha256": digest, "video_face_enhanced": True,
               "video_unenhanced_key": data["source_key"]}
    changed = db.query(VideoTask).filter_by(job_id=task.job_id, shot_number=task.shot_number,
                                           data_json=data["source_json"], status="done").update(
        {VideoTask.data_json: json.dumps(updated)}, synchronize_session=False)
    if changed != 1:
        db.rollback(); raise ValueError("Video changed during enhancement; current result was preserved.")
    data.update(status="done", output_key=stored["key"])
    changed = db.query(FaceEnhancement).filter_by(id=task_id, status="running").update(
        {FaceEnhancement.status: "done", FaceEnhancement.data_json: json.dumps(data), FaceEnhancement.heartbeat: time.time()}, synchronize_session=False)
    if changed != 1:
        db.rollback(); raise ValueError("Enhancement was interrupted; original result preserved.")
    db.commit()


def face_submission_slot(db, *, defer_seconds=None):
    """Shared account-wide CAS gate: no burst, one request per >=10.5 seconds."""
    import time
    from sqlalchemy.exc import IntegrityError
    now = time.time()
    row = db.get(ProviderSubmissionGate, "replicate-gfpgan", populate_existing=True)
    if row is None:
        db.add(ProviderSubmissionGate(provider="replicate-gfpgan", next_at=0))
        try: db.commit()
        except IntegrityError: db.rollback()
        return 0.1
    old = row.next_at
    if defer_seconds is None and old > now:
        return min(old-now, 5)
    target = max(old, now + defer_seconds) if defer_seconds is not None else now + 10.5
    changed = db.query(ProviderSubmissionGate).filter_by(provider=row.provider, next_at=old).update(
        {ProviderSubmissionGate.next_at: target}, synchronize_session=False)
    db.commit()
    return 0 if changed == 1 else 0.1


def claim_kling_voice(db, key, fields):
    from app.models import KlingVoice
    from sqlalchemy.exc import IntegrityError
    row = db.get(KlingVoice, key, populate_existing=True)
    if row is not None:
        return json.loads(row.data_json), False
    row = KlingVoice(key=key, status="submitting", data_json=json.dumps({**fields, "status": "submitting"}))
    db.add(row)
    try:
        db.commit()
        return json.loads(row.data_json), True
    except IntegrityError:
        db.rollback()
        return json.loads(db.get(KlingVoice, key, populate_existing=True).data_json), False


def read_kling_voice(db, key):
    from app.models import KlingVoice
    row = db.get(KlingVoice, key, populate_existing=True)
    return json.loads(row.data_json) if row else None


def update_kling_voice(db, key, **fields):
    from app.models import KlingVoice
    row = db.get(KlingVoice, key, populate_existing=True)
    data = {**json.loads(row.data_json), **fields}
    row.data_json, row.status = json.dumps(data), data["status"]
    db.commit()
    return data


def claim_kling_video_submission(db, job_id, number, task, request):
    """Only one worker may advance a prepared voice to a paid scene render."""
    row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).populate_existing().one()
    old = row.data_json
    data = json.loads(old)
    if row.status != "processing" or data.get("video_task_id") != task or data.get("video_phase") != "preparing_voice":
        return False
    from datetime import timezone
    data.update(video_phase="generating", video_status="submitting", video_retry_request=request,
                video_submitted_at=datetime.now(timezone.utc).isoformat())
    changed = db.query(VideoTask).filter_by(id=row.id, data_json=old).update(
        {VideoTask.status: "submitting", VideoTask.data_json: json.dumps(data)}, synchronize_session=False)
    db.commit()
    db.expire_all()
    return changed == 1
