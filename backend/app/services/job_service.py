import json
from typing import Any, Optional, get_args

from sqlalchemy.orm import Session

from app.models import AgentEvent, Job, VideoTask, FinalAssembly, FaceEnhancement, ProviderSubmissionGate
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
