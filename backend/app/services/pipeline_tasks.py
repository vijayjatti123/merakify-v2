"""Durable bounded dispatch to the existing Director, not a second pipeline."""
import asyncio
import logging
import queue
import threading

from app.db import SessionLocal
from app.services import job_service


def _run(job_id, token, kind, completed):
    with SessionLocal() as db:
        try:
            if kind == "plan":
                from app.agents.director import run_pipeline
                run_pipeline(db, job_id)
            elif kind == "prepare":
                from app.services.voice_generation_service import generate_job_dialogue_audio
                asyncio.run(generate_job_dialogue_audio(db, job_id))
            else:
                from app.agents.director import finalize_audio_assembly
                finalize_audio_assembly(db, job_id)
        except Exception as error:
            db.rollback()
            job_service.fail_pipeline_preparation(db, job_id, "Preparation could not finish. Your saved work is available; please retry.")
            job_service.append_event(db, job_id, "error", "Preparation failed: " + type(error).__name__)
        finally:
            job = job_service.get_job(db, job_id)
            completed.put((job_id, token, "failed" if job and job.status == "error" else "complete"))


def polling_loop(stop):
    active, completed = {}, queue.Queue()
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                while not completed.empty():
                    job_id, token, status = completed.get_nowait()
                    job_service.heartbeat_pipeline_task(db, job_id, token, status)
                    active.pop(job_id, None)
                for job_id, token in active.items():
                    job_service.heartbeat_pipeline_task(db, job_id, token)
                job_service.pause_expired_pipeline_tasks(db)
                while len(active) < 2:
                    task = job_service.claim_pipeline_task(db)
                    if not task:
                        break
                    job_id, token, kind = task
                    active[job_id] = token
                    threading.Thread(target=_run, args=(*task, completed), daemon=True, name="director-job").start()
        except Exception:
            logging.getLogger(__name__).exception("Pipeline dispatcher failed; queued work remains persisted")
        stop.wait(1)
