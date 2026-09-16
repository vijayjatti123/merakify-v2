import copy,json,unittest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import VideoTask
from app.services import video_generation_service as video, job_service as jobs

class RegenerateTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.shot=dict(shot_number=1,duration_sec=4,compiled_prompt='A glass under soft light.',still_frame_url='https://example.com/still.jpg',has_dialogue=False,characters_in_shot=[])
        self.result=dict(ai_model='Seedance 2.0',quality='480p',generation_approved=True,shots=[self.shot,{**self.shot,'shot_number':2}],continuity={})
        self.job=jobs.create_job(self.db,'audit',ai_model='Seedance 2.0',quality='480p');jobs.set_result(self.db,self.job.id,self.result);jobs.set_status(self.db,self.job.id,'done')
    def tearDown(self):self.db.close();self.engine.dispose()
    def seed(self,status='done'):
        jobs.claim_video(self.db,self.job.id,1,{'video_status':'submitting','video_submitted_at':'2026-09-13T00:00:00+00:00'})
        jobs.update_video(self.db,self.job.id,1,video_status=status,video_task_id='old',video_key='old.mp4' if status=='done' else None,video_requested_duration=4)
    def test_targeted_translation_and_full_fallback(self):
        with patch.object(video.storage_service,'asset_url',return_value='https://example.com/old.mp4'):
            edit=video.regenerate_translation(self.result,{**self.shot,'video_key':'old.mp4'},'warmer lighting')
            self.assertEqual(edit['request']['video_urls'],['https://example.com/old.mp4']);self.assertIn('warmer lighting',edit['request']['prompt'])
            full=video.regenerate_translation(self.result,self.shot,'warmer lighting');self.assertNotIn('video_urls',full['request']);self.assertIn('A glass',full['request']['prompt'])
            self.assertNotIn('video_urls',video.regenerate_translation(self.result,{**self.shot,'video_key':'old.mp4'})['request'])
    def test_one_shot_duplicate_archive_and_late_poll(self):
        self.seed();before=self.db.get(type(self.job),self.job.id).result_json
        with patch.object(video.storage_service,'asset_url',return_value='https://example.com/old.mp4'),patch.object(video,'provider',return_value={'id':'new','model':video.MODEL}) as call:
            video.start(self.db,self.job.id,1,regenerate=True,hint='warmer lighting',expected_attempt='old')
            with self.assertRaises(ValueError):video.start(self.db,self.job.id,1,regenerate=True,expected_attempt='old')
            self.assertEqual(call.call_count,1)
        self.assertEqual(before,self.db.get(type(self.job),self.job.id).result_json)
        self.assertEqual(self.db.query(VideoTask).count(),1)
        current=jobs.pending_videos(self.db)[0][1];self.assertEqual(current['video_task_id'],'new');self.assertEqual(current['video_attempt_history'][0]['video_key'],'old.mp4')
        jobs.update_video(self.db,self.job.id,1,expected_task_id='old',video_status='done',video_url='bad')
        self.assertEqual(jobs.pending_videos(self.db)[0][1]['video_task_id'],'new')
    def test_unknown_never_retries(self):
        self.seed('submission_unknown')
        with patch.object(video,'provider') as call:
            with self.assertRaises(ValueError):video.start(self.db,self.job.id,1,regenerate=True,expected_attempt='old')
            call.assert_not_called()
    def test_failed_no_video_uses_full_and_failure_is_visible(self):
        self.seed('failed')
        with patch.object(video,'provider',side_effect=RuntimeError('network')) as call:
            with self.assertRaises(RuntimeError):video.start(self.db,self.job.id,1,regenerate=True,expected_attempt='old')
            self.assertNotIn('video_urls',call.call_args.args[2])
        current=json.loads(self.db.query(VideoTask).one().data_json)
        self.assertEqual(current['video_status'],'submission_unknown');self.assertIsNone(current['video_url']);self.assertEqual(self.job.status,'done')
    def test_dialogue_dispatch_reuses_existing_audio(self):
        self.result['shots'][0].update(has_dialogue=True,speech_mode="onscreen",dialogue_text='Hello',dialogue_audio_url='https://example.com/audio.wav')
        jobs.set_result(self.db,self.job.id,self.result)
        from app.services import hedra_video_service as hedra,voice_generation_service as voice
        with patch.object(hedra,'start',return_value={'job_id':'new'}) as call,patch.object(voice,'generate_job_dialogue_audio') as tts:
            video.start(self.db,self.job.id,1,regenerate=True,expected_attempt='none')
            self.assertEqual(call.call_args.args[4]['dialogue_audio_url'],'https://example.com/audio.wav');tts.assert_not_called()

if __name__=='__main__':unittest.main()
