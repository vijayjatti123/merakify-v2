import json,time,unittest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import FaceEnhancement, VideoTask, ProviderSubmissionGate
from app.services import job_service as jobs
from app.services.video_generation_service import source_fingerprint

class FaceTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.storage=patch('app.services.storage_service.asset_url',side_effect=lambda key:'https://example.com/'+key);self.storage.start()
        self.job=jobs.create_job(self.db,'audit');self.job.status='done'
        self.shot={'shot_number':1,'has_dialogue':True,'compiled_prompt':'Meera speaking','duration_sec':2}
        self.job.result_json=json.dumps({'shots':[self.shot]});self.db.commit()
        self.fields={'video_task_id':'original-task','video_key':'original.mp4','video_url':'https://example.com/original.mp4','video_status':'done','video_provider':'hedra','video_source_hash':source_fingerprint(self.shot)}
        self.db.add(VideoTask(job_id=self.job.id,shot_number=1,status='done',data_json=json.dumps(self.fields)));self.db.commit()
    def tearDown(self):
        self.storage.stop();self.db.close();self.engine.dispose()
    def claim(self):return jobs.claim_face_enhancement(self.db,self.job.id,1,'original.mp4')['enhancement_id']
    def test_duplicate_and_opt_in_original_preserved(self):
        original=self.db.query(VideoTask).one().data_json;task=self.claim()
        with self.assertRaisesRegex(ValueError,'already'):self.claim()
        self.assertEqual(self.db.query(VideoTask).one().data_json,original)
        result=jobs.job_result(self.job);self.assertEqual(result['shots'][0]['video_key'],'original.mp4');self.assertEqual(result['shots'][0]['face_enhancement']['status'],'queued')
    def test_success_atomic_swap_and_failed_original(self):
        task=self.claim();self.assertEqual(jobs.next_face_enhancement(self.db),task)
        jobs.face_progress(self.db,task,status='failed',warning='provider failed')
        self.assertEqual(self.db.query(VideoTask).one().data_json,json.dumps(self.fields))
        self.claim();jobs.next_face_enhancement(self.db)
        jobs.finish_face_enhancement(self.db,task,{'key':'enhanced.mp4','url':'https://example.com/enhanced.mp4'},'digest')
        self.db.expire_all();data=json.loads(self.db.query(VideoTask).one().data_json);self.assertEqual(data['video_key'],'enhanced.mp4');self.assertEqual(data['video_unenhanced_key'],'original.mp4')
    def test_regeneration_race_does_not_overwrite(self):
        task=self.claim();jobs.next_face_enhancement(self.db)
        jobs.update_video(self.db,self.job.id,1,video_key='new.mp4',video_task_id='new-task')
        with self.assertRaisesRegex(ValueError,'changed'):jobs.finish_face_enhancement(self.db,task,{'key':'enhanced.mp4','url':'https://example.com/enhanced.mp4'},'digest')
        self.db.expire_all();self.assertEqual(json.loads(self.db.query(VideoTask).one().data_json)['video_key'],'new.mp4')
    def test_non_dialogue_or_stale_rejected(self):
        self.fields['video_provider']='evolink';jobs.update_video(self.db,self.job.id,1,**self.fields)
        with self.assertRaisesRegex(ValueError,'Hedra'):self.claim()
    def test_shared_submission_gate_and_429_deferral(self):
        jobs.face_submission_slot(self.db)
        self.assertEqual(jobs.face_submission_slot(self.db),0)
        self.assertGreater(jobs.face_submission_slot(self.db),0)
        with Session(self.engine) as other:self.assertGreater(jobs.face_submission_slot(other),0)
        jobs.face_submission_slot(self.db,defer_seconds=30)
        self.db.expire_all();self.assertGreater(self.db.get(ProviderSubmissionGate,'replicate-gfpgan').next_at,time.time()+29)
    def test_late_worker_cannot_modify_retried_attempt(self):
        task=self.claim();jobs.next_face_enhancement(self.db)
        token=json.loads(self.db.get(FaceEnhancement,task).data_json)['run_token']
        jobs.face_progress(self.db,task,status='failed',warning='interrupted')
        self.claim();jobs.next_face_enhancement(self.db)
        with self.assertRaisesRegex(ValueError,'superseded'):
            jobs.face_progress(self.db,task,expected_run_token=token,status='failed')
        with self.assertRaisesRegex(ValueError,'superseded'):
            jobs.finish_face_enhancement(self.db,task,{'key':'old-result','url':'https://example.com/old'},'digest',expected_run_token=token)
        self.db.expire_all();self.assertEqual(self.db.get(FaceEnhancement,task).status,'running')

    def test_stopped_worker_fails_preserving_original(self):
        task=self.claim();jobs.next_face_enhancement(self.db)
        self.db.query(FaceEnhancement).update({'heartbeat':time.time()-301});self.db.commit();jobs.next_face_enhancement(self.db)
        self.db.expire_all();self.assertEqual(self.db.get(FaceEnhancement,task).status,'failed');self.assertEqual(json.loads(self.db.query(VideoTask).one().data_json)['video_key'],'original.mp4')

if __name__=='__main__':unittest.main()
