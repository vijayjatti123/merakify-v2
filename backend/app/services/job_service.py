import json
from typing import Any, Optional, get_args

from sqlalchemy.orm import Session

from app.models import AgentEvent, Job, VideoTask
from app.schemas import ColorGrade, VisualStyle, style_from_brief


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
    script_text: str | None = None,
    resolutions: dict | None = None,
) -> Job:
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
    job = Job(
        brief=brief,
        aspect_ratio=aspect_ratio,
        visual_style=visual_style,
        color_grade=color_grade,
        quality=quality,
        language=language,
        ai_model=ai_model,
        script_text=script_text,
        resolutions_json=json.dumps(resolutions, ensure_ascii=False) if resolutions is not None else None,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str) -> Optional[Job]:
    return db.query(Job).filter(Job.id == job_id).first()


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
        from app.services import storage_service
        from app.services.still_frame_service import invalidate_changed_stills
        invalidate_changed_stills(result)
        for shot in result.get("shots", []):
            if shot.get("still_frame_key"):
                try:
                    shot["still_frame_url"] = storage_service.asset_url(shot["still_frame_key"])
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
    return result


def video_source(db, job_id, number):
    job = get_job(db, job_id)
    if not job:
        raise LookupError("Job not found")
    result = job_result(job) or {}
    if job.status != "done" or result.get("audio_assembly_pending") or result.get("assembly", {}).get("provisional"):
        raise ValueError("Finish final shot planning and audio approval first")
    result.update(ai_model=job.ai_model, quality=job.quality, aspect_ratio=job.aspect_ratio)
    shot = next((s for s in result.get("shots", []) if s.get("shot_number") == number), None)
    if shot is None:
        raise LookupError("Shot not found")
    return result, shot


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
    row = db.query(VideoTask).filter_by(job_id=job_id, shot_number=number).with_for_update().populate_existing().one()
    data = json.loads(row.data_json)
    expected_task = fields.pop("expected_task_id", None)
    expected_started = fields.pop("expected_submitted_at", None)
    if ((expected_task is not None and data.get("video_task_id") != expected_task)
            or (expected_started is not None and data.get("video_submitted_at") != expected_started)):
        db.rollback()
        return False  # A late poll must never overwrite a newer paid attempt.
    data.update(fields)
    row.data_json = json.dumps(data)
    row.status = data["video_status"]
    db.commit()


def pending_videos(db):
    return [(row.job_id, {**json.loads(row.data_json), "shot_number": int(row.shot_number)})
            for row in db.query(VideoTask).filter(VideoTask.status.in_(["processing", "submitting"])).all()]
