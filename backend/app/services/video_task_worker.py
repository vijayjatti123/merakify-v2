"""Bounded dispatch for the existing video pipeline; no provider submissions here."""
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

from app.db import SessionLocal
from app.services import job_service

POLL_WORKERS = 4
MEDIA_WORKERS = 3
POLL_INTERVAL = 10
log = logging.getLogger(__name__)


@dataclass
class RetryWork:
    delay: float = 10
    rate_limited: bool = False


def retry_details(error):
    cause = error
    while cause:
        status = getattr(cause, 'code', None) or getattr(getattr(cause, 'response', None), 'status_code', None)
        if status == 429:
            headers = getattr(cause, 'headers', None) or getattr(getattr(cause, 'response', None), 'headers', {})
            value = headers.get('Retry-After', '')
            try:
                delay = float(value)
            except (ValueError, TypeError):
                try:
                    delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            return RetryWork(max(10, delay), True)
        cause = cause.__cause__
    return RetryWork()


def work(job_id, shot, response=None, completion=None):
    from app.services import video_generation_service as video
    with SessionLocal() as db:
        try:
            if response is None:
                return video.poll(db, job_id, shot, defer_completed=True)
            return video.finish_completed(db, job_id, shot, response, completion=completion)
        except Exception as error:
            db.rollback()
            message = f"Video polling/storage temporarily failed: {type(error).__name__}; saved task will be polled again."
            if shot.get("video_error") != message:
                job_service.update_video(db, job_id, shot['shot_number'],
                    expected_task_id=shot.get('video_task_id'), expected_submitted_at=shot.get('video_submitted_at'),
                    video_error=message)
                job_service.append_event(db, job_id, "video_generation", f"Shot {shot['shot_number']}: {message}")
            return retry_details(error)


class Dispatcher:
    def __init__(self):
        self.pollers = ThreadPoolExecutor(POLL_WORKERS, thread_name_prefix="video-status")
        self.media = ThreadPoolExecutor(MEDIA_WORKERS, thread_name_prefix="video-media")
        self.active = {}
        self.due = {}
        self.failures = {}
        self.cooldowns = {}

    def discard(self, key):
        entry = self.active.pop(key)
        if entry.get('completion'):
            entry['completion'].close()

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
                        response = RetryWork()
                    if isinstance(response, RetryWork):
                        self.failures[key] = self.failures.get(key, 0) + 1
                        delay = max(response.delay, min(60, 10 * 2 ** min(self.failures[key] - 1, 3)))
                        if response.rate_limited and entry['stage'] == 'poll':
                            provider = shot.get('video_provider', 'evolink')
                            self.cooldowns[provider] = max(self.cooldowns.get(provider, 0), now + delay)
                        if entry['stage'] == 'media' and self.failures[key] <= 3:
                            entry.update(stage='ready', retry_at=now + delay, ready_at=now)
                            continue
                        job_service.video_worker_lease(db, job, number, None, token, release=True)
                        self.discard(key)
                        self.due[key] = now + delay
                        continue
                    if entry['stage'] == 'poll' and response is not None:
                        self.failures.pop(key, None)
                        entry.update(stage='ready', response=response, ready_at=now)
                    else:
                        job_service.video_worker_lease(db, job, number, None, token, release=True)
                        # Ten seconds between starts, not ten more after a slow GET.
                        self.due[key] = max(now, entry['poll_started'] + POLL_INTERVAL)
                        self.discard(key)
                        self.failures.pop(key, None)
                        continue
                if now - entry['heartbeat'] >= 10:
                    owned = job_service.video_worker_lease(db, job, number, None, token, renew=True)
                    if owned is None:
                        # Never dispatch queued media after losing ownership.
                        if entry['stage'] == 'ready':
                            self.discard(key)
                        log.warning("Video worker lease renewal failed")
                        continue
                    entry['heartbeat'] = now

            free_media = MEDIA_WORKERS - sum(e['stage'] == 'media' for e in self.active.values())
            for (job, number), entry in list(self.active.items()):
                if free_media <= 0:
                    break
                if entry['stage'] != 'ready' or entry.get('retry_at', 0) > now:
                    continue
                current = job_service.video_worker_lease(db, job, number, None, entry['token'], renew=True)
                if current is None or current.get('video_task_id') != entry['shot'].get('video_task_id'):
                    self.discard((job, number))
                    continue
                job_service.append_event(db, job, 'video_timing',
                    f"Shot {number}: completed video dispatch wait {now - entry['ready_at']:.3f}s")
                from app.services.video_generation_service import CompletionCache
                if 'completion' not in entry:
                    entry['completion'] = CompletionCache(current['video_task_id'])
                entry.update(stage='media', future=self.media.submit(work, job, current, entry['response'], entry['completion']))
                free_media -= 1

            # Bound both live status work and its completed-response backlog.
            free_poll = POLL_WORKERS - sum(e['stage'] in {'poll', 'ready'} for e in self.active.values())
            pending = job_service.pending_videos(db)
            keys = {(j, s['shot_number']) for j, s in pending}
            self.due = {k: v for k, v in self.due.items() if k in keys}
            self.failures = {k: v for k, v in self.failures.items() if k in keys}
            for job, shot in pending:
                key = job, shot['shot_number']
                if free_poll <= 0:
                    break
                if (key in self.active or self.due.get(key, 0) > now
                        or self.cooldowns.get(shot.get('video_provider', 'evolink'), 0) > now):
                    continue
                token = uuid.uuid4().hex
                claimed = job_service.video_worker_lease(db, job, key[1], shot.get('video_task_id'), token)
                if claimed is None:
                    continue
                self.active[key] = dict(shot=claimed, token=token, stage='poll', heartbeat=now, poll_started=time.monotonic(),
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
        for key in list(self.active):
            self.discard(key)


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
