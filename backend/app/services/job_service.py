import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import AgentEvent, Job


def create_job(
    db: Session,
    brief: str,
    *,
    aspect_ratio: str = "16:9",
    quality: str = "720p",
    language: str = "English",
    ai_model: str = "Seedance 2.5",
    script_text: str | None = None,
    resolutions: dict | None = None,
) -> Job:
    job = Job(
        brief=brief,
        aspect_ratio=aspect_ratio,
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
    return json.loads(job.result_json) if job.result_json else None
