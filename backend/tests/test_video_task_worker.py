import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, Mock
from concurrent.futures import Future

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import VideoTask
from app.services import job_service as jobs, video_task_worker as worker


class VideoWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.tmp.name)/'jobs.db'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.job = jobs.create_job(db, 'isolated worker test').id
            for n in range(1, 8):
                jobs.claim_video(db, self.job, n, dict(video_status='processing', video_task_id=str(n),
                    video_submitted_at=datetime.now(timezone.utc).isoformat()))
                jobs.update_video(db,self.job,n,video_status='processing')
        p=patch.object(worker,'SessionLocal',self.sessions);p.start();self.addCleanup(p.stop)

    def test_lease_exclusion_expiry_and_old_owner_release(self):
        with self.sessions() as db:
            self.assertIsNotNone(jobs.video_worker_lease(db,self.job,1,'1','one'))
            self.assertIsNone(jobs.video_worker_lease(db,self.job,1,'1','two'))
            self.assertIsNone(jobs.video_worker_lease(db,self.job,1,'wrong','one'))
            row=db.query(VideoTask).filter_by(job_id=self.job,shot_number=1).one()
            data=json.loads(row.data_json)
            data['video_worker_lease']['until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            row.data_json=json.dumps(data);db.commit()
            self.assertIsNotNone(jobs.video_worker_lease(db,self.job,1,'1','two'))
            self.assertIsNone(jobs.video_worker_lease(db,self.job,1,None,'one',release=True))
            self.assertIsNotNone(jobs.video_worker_lease(db,self.job,1,None,'two',release=True))

    def test_blocked_media_does_not_block_other_polls_and_bounds_are_respected(self):
        release=threading.Event(); polled=set(); counts={'media':0,'peak':0}; lock=threading.Lock()
        def fake_work(job,shot,response=None,completion=None):
            if response is None:
                with lock: polled.add(shot['shot_number'])
                return {'status':'completed'}
            with lock:
                counts['media']+=1;counts['peak']=max(counts['peak'],counts['media'])
            release.wait(5)
            with self.sessions() as db: jobs.update_video(db,job,shot['shot_number'],video_status='done')
            with lock: counts['media']-=1
        dispatcher=worker.Dispatcher()
        try:
            with patch.object(worker,'work',side_effect=fake_work):
                deadline=time.monotonic()+3
                while len(polled)<7 and time.monotonic()<deadline:
                    dispatcher.tick();time.sleep(.02)
                self.assertEqual(len(polled),7)
                self.assertEqual(counts['peak'],3)
                self.assertLessEqual(len(dispatcher.active),7)
                with self.sessions() as db:
                    self.assertIsNone(jobs.video_worker_lease(db,self.job,1,'1','competing-process'))
                release.set()
                while dispatcher.active and time.monotonic()<deadline+3:
                    dispatcher.tick();time.sleep(.02)
                self.assertFalse(dispatcher.active)
        finally:
            release.set();dispatcher.close()

    def test_worker_failure_is_persisted_and_recoverable(self):
        from app.services import video_generation_service as video
        with self.sessions() as db: shot=jobs.pending_videos(db)[0][1]
        with patch.object(video,'poll',side_effect=RuntimeError('private')):
            worker.work(self.job,shot)
        with self.sessions() as db:
            data=jobs.pending_videos(db)[0][1]
            self.assertIn('RuntimeError',data['video_error'])
            self.assertNotIn('private',data['video_error'])
            self.assertEqual(data['video_status'],'processing')

    def test_locked_audio_speech_only_review_resumes_existing_task(self):
        speech_only = {'verdict': {
            'style': {'status': 'pass'}, 'scale': {'status': 'pass'},
            'staging': {'status': 'pass'}, 'speech': {'status': 'mismatch'}}}
        with self.sessions() as db:
            for number in range(2, 8):
                jobs.update_video(db, self.job, number, video_status='done')
            jobs.update_video(db, self.job, 1, video_status='review_required',
                video_error='speech mismatch',
                video_audio_lock={'policy':'approved-dialogue-plus-silence-v1'},
                video_compliance_checks={'1': speech_only})
            pending = jobs.pending_videos(db)
            self.assertEqual([(job, shot['shot_number']) for job, shot in pending], [(self.job, 1)])
            claimed = jobs.video_worker_lease(db, self.job, 1, '1', 'recovery')
            self.assertEqual(claimed['video_status'], 'processing')
            self.assertIsNone(claimed['video_error'])

    def test_visual_review_never_resumes_automatically(self):
        visual = {'verdict': {
            'style': {'status': 'mismatch'}, 'scale': {'status': 'pass'},
            'staging': {'status': 'pass'}, 'speech': {'status': 'pass'}}}
        with self.sessions() as db:
            for number in range(2, 8):
                jobs.update_video(db, self.job, number, video_status='done')
            jobs.update_video(db, self.job, 1, video_status='review_required',
                video_audio_lock={'policy':'approved-dialogue-plus-silence-v1'},
                video_compliance_checks={'1': visual})
            self.assertEqual(jobs.pending_videos(db), [])
            self.assertIsNone(jobs.video_worker_lease(db, self.job, 1, '1', 'blocked'))

    def test_compliance_save_survives_concurrent_heartbeat(self):
        from sqlalchemy.orm import Query
        update = Query.update
        collided = []
        def racing_update(query, values, **kwargs):
            if not collided:
                collided.append(True)
                with self.sessions() as other:
                    self.assertIsNotNone(jobs.video_worker_lease(other,self.job,1,'1','heartbeat'))
            return update(query, values, **kwargs)
        with self.sessions() as db, patch.object(Query, 'update', racing_update):
            result = jobs.video_check_state(db,self.job,1,'1',check={'verdict':'already-computed'})
        self.assertIsNotNone(result)
        self.assertEqual(result['video_compliance_checks']['1'], {'verdict':'already-computed'})
        self.assertEqual(result['video_worker_lease']['token'], 'heartbeat')

    def test_upload_retry_reuses_download_check_and_completed_response(self):
        from app.services import video_generation_service as video, render_compliance_service as gate
        from test_render_compliance import verdict
        class ImmediatePool:
            def __init__(self, *args, **kwargs): pass
            def submit(self, fn, *args):
                result = Future()
                try: result.set_result(fn(*args))
                except Exception as error: result.set_exception(error)
                return result
            def shutdown(self, **kwargs): pass
        with self.sessions() as db:
            for number in range(2,8):
                jobs.update_video(db,self.job,number,video_status='done')
            jobs.update_video(db,self.job,1,video_compliance_expected={'visual_style':'Natural'})
        download=Mock()
        download.__enter__=Mock(return_value=download)
        download.__exit__=Mock(return_value=False)
        download.iter_bytes.return_value=[b'0000ftyp0000synthetic-video']
        clock=[100.0]
        with patch.object(worker,'ThreadPoolExecutor',ImmediatePool), \
             patch.object(worker.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(video,'poll',return_value={'status':'completed','results':['https://provider/video']}) as poll, \
             patch.object(video.httpx,'stream',return_value=download) as fetch, \
             patch.object(gate,'inspect',return_value=verdict()) as inspect, \
             patch.object(video.storage_service,'upload_file',side_effect=[OSError('temporary storage outage'), {'url':'https://stored/video','key':'saved'}]) as upload:
            dispatcher=worker.Dispatcher()
            try:
                dispatcher.tick()  # poll
                clock[0]=101;dispatcher.tick()  # first media attempt fails upload
                cache=dispatcher.active[(self.job,1)]['completion']
                clock[0]=102;dispatcher.tick()  # retains cache for retry
                clock[0]=113;dispatcher.tick()  # second media attempt
                clock[0]=114;dispatcher.tick()  # releases cache
                self.assertFalse(dispatcher.active)
                self.assertTrue(cache.media.closed)
                poll.assert_called_once();fetch.assert_called_once();inspect.assert_called_once()
                self.assertEqual(upload.call_count,2)
            finally: dispatcher.close()
        with self.sessions() as db:
            data=json.loads(db.query(VideoTask).filter_by(job_id=self.job,shot_number=1).one().data_json)
            self.assertEqual(data['video_status'],'done')
            self.assertTrue(data['video_processing_timings']['download_reused'])

    def test_poll_due_is_start_based_and_provider_backoff_is_honored(self):
        from urllib.error import HTTPError
        from email.message import Message
        headers=Message();headers['Retry-After']='45'
        error=RuntimeError('safe');error.__cause__=HTTPError('https://provider',429,'slow down',headers,None)
        retry=worker.retry_details(error)
        self.assertEqual(retry.delay,45)
        self.assertTrue(retry.rate_limited)
        # A six-second GET need not add another ten seconds on top.
        from concurrent.futures import Future
        with self.sessions() as db:
            for number in range(2,8): jobs.update_video(db,self.job,number,video_status='done')
            shot=jobs.video_worker_lease(db,self.job,1,'1','owner')
        done=Future();done.set_result(None)
        dispatcher=worker.Dispatcher()
        try:
            dispatcher.active[(self.job,1)]={'shot':shot,'token':'owner','stage':'poll','future':done,'heartbeat':100,'poll_started':100}
            with patch.object(worker.time,'monotonic',return_value=106): dispatcher.tick()
            self.assertEqual(dispatcher.due[(self.job,1)],110)
        finally: dispatcher.close()

    def test_completion_cache_cannot_cross_provider_tasks(self):
        from app.services import video_generation_service as video
        cache=video.CompletionCache('old')
        try:
            with patch.object(video.httpx,'stream') as download:
                with self.assertRaisesRegex(ValueError,'another provider task'):
                    video.finish_completed(None,self.job,{'shot_number':1,'video_task_id':'new'},
                                           {'results':['https://provider/video']},completion=cache)
                download.assert_not_called()
        finally: cache.close()


if __name__=='__main__':unittest.main()
