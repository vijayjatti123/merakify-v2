"""Bounded dispatch for the existing video pipeline; no provider submissions here."""
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.db import SessionLocal
from app.services import job_service

POLL_WORKERS = 4
MEDIA_WORKERS = 3
POLL_INTERVAL = 10
log = logging.getLogger(__name__)


def work(job_id, shot, response=None):
    from app.services import video_generation_service as video
    with SessionLocal() as db:
        try:
            if response is None:
                return video.poll(db, job_id, shot, defer_completed=True)
            return video.finish_completed(db, job_id, shot, response)
        except Exception as error:
            db.rollback()
            message = f"Video polling/storage temporarily failed: {type(error).__name__}; saved task will be polled again."
            if shot.get("video_error") != message:
                job_service.update_video(db, job_id, shot['shot_number'],
                    expected_task_id=shot.get('video_task_id'), expected_submitted_at=shot.get('video_submitted_at'),
                    video_error=message)
                job_service.append_event(db, job_id, "video_generation", f"Shot {shot['shot_number']}: {message}")
            return None


class Dispatcher:
    def __init__(self):
        self.pollers = ThreadPoolExecutor(POLL_WORKERS, thread_name_prefix="video-status")
        self.media = ThreadPoolExecutor(MEDIA_WORKERS, thread_name_prefix="video-media")
        self.active = {}
        self.due = {}

    def tick(self):
        now = time.monotonic()
        with SessionLocal() as db:
            for key, entry in list(self.active.items()):
                job, number = key
                shot, token = entry['shot'], entry['token']
                future = entry['future']
                if entry['stage'] != 'ready' and future.done():
                    try:
                        response = future.result()
                    except Exception:
                        log.exception("Video worker failed; persisted task remains recoverable")
                        response = None
                    if entry['stage'] == 'poll' and response is not None:
                        entry.update(stage='ready', response=response, ready_at=now)
                    else:
                        job_service.video_worker_lease(db, job, number, None, token, release=True)
                        del self.active[key]
                        self.due[key] = now + POLL_INTERVAL
                        continue
                if now - entry['heartbeat'] >= 10:
                    owned = job_service.video_worker_lease(db, job, number, None, token, renew=True)
                    if owned is None:
                        # Never dispatch queued media after losing ownership.
                        if entry['stage'] == 'ready':
                            del self.active[key]
                        log.warning("Video worker lease renewal failed")
                        continue
                    entry['heartbeat'] = now

            free_media = MEDIA_WORKERS - sum(e['stage'] == 'media' for e in self.active.values())
            for (job, number), entry in list(self.active.items()):
                if free_media <= 0:
                    break
                if entry['stage'] != 'ready':
                    continue
                current = job_service.video_worker_lease(db, job, number, None, entry['token'], renew=True)
                if current is None or current.get('video_task_id') != entry['shot'].get('video_task_id'):
                    self.active.pop((job, number))
                    continue
                job_service.append_event(db, job, 'video_timing',
                    f"Shot {number}: completed video dispatch wait {now - entry['ready_at']:.3f}s")
                entry.update(stage='media', future=self.media.submit(work, job, current, entry['response']))
                free_media -= 1

            # Bound both live status work and its completed-response backlog.
            free_poll = POLL_WORKERS - sum(e['stage'] in {'poll', 'ready'} for e in self.active.values())
            pending = job_service.pending_videos(db)
            keys = {(j, s['shot_number']) for j, s in pending}
            self.due = {k: v for k, v in self.due.items() if k in keys}
            for job, shot in pending:
                key = job, shot['shot_number']
                if free_poll <= 0:
                    break
                if key in self.active or self.due.get(key, 0) > now:
                    continue
                token = uuid.uuid4().hex
                claimed = job_service.video_worker_lease(db, job, key[1], shot.get('video_task_id'), token)
                if claimed is None:
                    continue
                self.active[key] = dict(shot=claimed, token=token, stage='poll', heartbeat=now,
                                        future=self.pollers.submit(work, job, claimed))
                free_poll -= 1

    def close(self):
        # No queued executor backlog: every submitted item has a reserved slot.
        # Keep leases alive while graceful shutdown drains in-flight work.
        # A hard process kill instead recovers through the lease expiry.
        while any(e['stage'] != 'ready' and not e['future'].done() for e in self.active.values()):
            try:
                with SessionLocal() as db:
                    for (job, number), entry in self.active.items():
                        job_service.video_worker_lease(db, job, number, None, entry['token'], renew=True)
            except Exception:
                log.exception("Video shutdown lease renewal failed")
            time.sleep(1)
        self.pollers.shutdown(wait=True, cancel_futures=True)
        self.media.shutdown(wait=True, cancel_futures=True)
        with SessionLocal() as db:
            for (job, number), entry in self.active.items():
                job_service.video_worker_lease(db, job, number, None, entry['token'], release=True)


def run(stop):
    dispatcher = Dispatcher()
    try:
        while not stop.is_set():
            try:
                dispatcher.tick()
            except Exception:
                log.exception("Video dispatcher failed; persisted tasks will be scanned again")
            stop.wait(1)
    finally:
        dispatcher.close()
