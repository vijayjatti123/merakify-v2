import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

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
        def fake_work(job,shot,response=None):
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


if __name__=='__main__':unittest.main()
