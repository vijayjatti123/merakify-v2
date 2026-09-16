"""Missing-still recovery: isolated DB and injected provider boundaries, no paid calls."""
import asyncio
import copy
import io
import json
import unittest
from unittest.mock import patch
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models import Job
from app.services import job_service as jobs, still_frame_service as still, video_generation_service as video
from app.services.character_image_service import GeneratedCharacterImage
from app.routes import jobs as routes

class StillRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        buffer = io.BytesIO()
        still.Image.new('RGB', (160, 90)).save(buffer, format='PNG')
        self.image = GeneratedCharacterImage(buffer.getvalue(), 'image/png')
        self.result = {'generation_approved': True, 'assembly': {}, 'continuity': {'characters': []}, 'shots': [
            {'shot_number': 1, 'scene_number': 1, 'compiled_prompt': 'Coffee filter close-up.', 'has_dialogue': False,
             'duration_sec': 5, 'characters_in_shot': [], 'still_frame_status': 'failed', 'still_frame_url': None},
            {'shot_number': 2, 'scene_number': 2, 'compiled_prompt': 'Neighbor unchanged.', 'still_frame_url': 'https://audit/neighbor'}]}
        self.job = Job(brief='TEST', status='done', ai_model='Seedance 2.0', result_json=json.dumps(self.result))
        self.db.add(self.job); self.db.commit()

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def run_recovery(self, verdict):
        task = BackgroundTasks()
        with patch.object(routes, 'SessionLocal', self.sessions), patch.object(still, 'generate_still', return_value=self.image), \
             patch.object(still, 'check_still', return_value=verdict), \
             patch.object(still.storage_service, 'upload_bytes', return_value={'key':'audit/still','url':'https://audit/still'}), \
             patch.object(still.storage_service, 'asset_url', return_value='https://audit/still'), \
             patch.object(video, 'provider', return_value={'id':'test-task','model':video.MODEL}) as provider:
            response = routes.regenerate_video(self.job.id, 1, routes.VideoRegenerateRequest(expected_attempt='none'), task, self.db)
            self.assertEqual(response['status'], 'generating_still')
            asyncio.run(task())
            self.db.expire_all()
            return jobs.job_result(jobs.get_job(self.db, self.job.id)), provider.call_count

    def test_double_rejection_has_no_usable_output_and_no_video_call(self):
        result, calls = self.run_recovery({'approved':False, 'reason':'Deliberate framing mismatch'})
        self.assertEqual(calls, 0)
        self.assertEqual(result['shots_needing_attention'], [1])
        self.assertEqual(result['shots'][0]['still_frame_status'], 'failed')
        self.assertIsNone(result['shots'][0]['still_frame_url'])
        self.assertFalse(result['shots'][0].get('video_url'))
        self.assertEqual(result['shots'][1], self.result['shots'][1])
        messages = [e.note for e in jobs.get_events_since(self.db, self.job.id)]
        self.assertEqual(sum('Reason: Deliberate framing mismatch' in m for m in messages), 2)
        self.assertNotIn('Continuing with compiled text', str(messages))

    def test_success_reuses_existing_video_path_and_only_target_changes(self):
        result, calls = self.run_recovery({'approved':True, 'reason':'Matches'})
        self.assertEqual(calls, 1)
        self.assertEqual(result['shots'][0]['still_frame_status'], 'ready')
        self.assertEqual(result['shots'][0]['video_status'], 'processing')
        self.assertEqual(result['shots_needing_attention'], [])
        self.assertEqual(result['shots'][1], self.result['shots'][1])

    def test_duplicate_claim_is_rejected(self):
        jobs.claim_still_retry(self.db,self.job.id,1,'none')
        with self.assertRaisesRegex(ValueError,'already regenerating'):
            jobs.claim_still_retry(self.db,self.job.id,1,'none')

    def test_preview_retry_never_starts_video(self):
        task = BackgroundTasks()
        with patch.object(routes, 'SessionLocal', self.sessions), \
             patch.object(still, 'generate_still', return_value=self.image), \
             patch.object(still, 'check_still', return_value={'approved': True, 'reason': 'Matches'}), \
             patch.object(still.storage_service, 'upload_bytes', return_value={'key':'audit/still','url':'https://audit/still'}), \
             patch.object(video, 'start') as start:
            response = routes.retry_preview(self.job.id, 1, routes.VideoRegenerateRequest(expected_attempt='none'), task, self.db)
            self.assertEqual(response['status'], 'generating')
            asyncio.run(task())
            start.assert_not_called()
        self.db.expire_all()
        result = jobs.job_result(jobs.get_job(self.db, self.job.id))
        self.assertEqual(result['shots'][0]['still_frame_status'], 'ready')
        self.assertFalse(result['shots'][0].get('video_url'))
        self.assertEqual(result['shots'][1], self.result['shots'][1])

    def test_live_progress_reports_start_and_completion(self):
        seen = []
        with patch.object(still, 'generate_still', return_value=self.image), \
             patch.object(still, 'check_still', return_value={'approved': False, 'reason': 'Deliberate mismatch'}):
            still.generate_still_frames(self.result, job_id=self.job.id, emit=lambda *args: None,
                shot_numbers={1}, on_progress=lambda result: seen.append(result['shots'][0]['still_frame_status']))
        self.assertEqual(seen, ['generating', 'failed'])

    def test_stale_token_cannot_save(self):
        jobs.claim_still_retry(self.db,self.job.id,1,'none')
        self.assertFalse(jobs.finish_still_retry(self.db,self.job.id,1,'stale',self.result))

    def test_initial_generation_is_not_an_expired_manual_retry(self):
        self.result['shots'][0]['still_frame_status'] = 'generating'
        self.job.result_json = json.dumps(self.result)
        self.db.commit()
        self.assertEqual(jobs.job_result(self.job)['shots'][0]['still_frame_status'], 'generating')

    def test_expired_manual_retry_still_reads_as_failed(self):
        self.result['shots'][0].update(still_frame_status='generating', still_retry_token='audit',
            still_retry_started_at='2000-01-01T00:00:00+00:00')
        self.job.result_json = json.dumps(self.result)
        self.db.commit()
        self.assertEqual(jobs.job_result(self.job)['shots'][0]['still_frame_status'], 'failed')

    def test_preparation_retry_keeps_audio_and_rejects_second_claim(self):
        self.result['shots'] = [dict(self.result['shots'][0], has_dialogue=True,
            status='done', dialogue_audio_url='https://audit/saved-speech', dialogue_voice_id='priya')]
        self.job.result_json = json.dumps(self.result)
        self.job.status = 'error'
        self.job.error_message = 'Shot Prompt Compiler deadline exceeded'
        self.db.commit()
        jobs.claim_preview_preparation(self.db, self.job.id)
        restored = jobs.job_result(jobs.get_job(self.db, self.job.id))
        self.assertTrue(restored['audio_assembly_pending'])
        self.assertEqual(restored['shots'][0]['dialogue_audio_url'], 'https://audit/saved-speech')
        self.assertIsNone(jobs.get_job(self.db, self.job.id).error_message)
        with self.assertRaisesRegex(ValueError, 'already running'):
            jobs.claim_preview_preparation(self.db, self.job.id)

    def test_preparation_retry_cannot_overwrite_existing_images(self):
        self.job.status = 'error'; self.db.commit()
        with self.assertRaisesRegex(ValueError, 'preserve existing output'):
            jobs.claim_preview_preparation(self.db, self.job.id)

    def test_unapproved_plan_is_not_submitted(self):
        self.result['generation_approved'] = False
        self.job.result_json=json.dumps(self.result);self.db.commit()
        with self.assertRaisesRegex(ValueError,'Approve'):
            jobs.claim_still_retry(self.db,self.job.id,1,'none')

    def test_existing_video_is_preserved_when_preview_failed(self):
        self.result['shots'][0]['video_url']='https://audit/existing-video'
        self.job.result_json=json.dumps(self.result);self.db.commit()
        self.assertEqual(jobs.job_result(self.job)['shots_needing_attention'], [])
        with self.assertRaisesRegex(ValueError,'output changed'):
            jobs.claim_still_retry(self.db,self.job.id,1,'none')

if __name__ == '__main__':
    unittest.main()

