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

    def test_visual_mismatch_preserves_image_without_starting_video(self):
        result, calls = self.run_recovery({'approved':False, 'reason':'Deliberate framing mismatch'})
        self.assertEqual(calls, 0)
        self.assertEqual(result['shots_needing_attention'], [])
        self.assertEqual(result['shots'][0]['still_frame_status'], 'ready')
        self.assertEqual(result['shots'][0]['still_frame_retry_feedback'], 'Deliberate framing mismatch')
        self.assertEqual(result['shots'][0]['still_frame_url'], 'https://audit/still')
        self.assertFalse(result['shots'][0].get('video_url'))
        self.assertEqual(result['shots'][1], self.result['shots'][1])
        messages = [e.note for e in jobs.get_events_since(self.db, self.job.id)]
        self.assertEqual(sum('Reason: Deliberate framing mismatch' in m for m in messages), 1)
        self.assertNotIn('Continuing with compiled text', str(messages))

    def test_success_reuses_existing_video_path_and_only_target_changes(self):
        result, calls = self.run_recovery({'approved':True, 'reason':'Matches'})
        self.assertEqual(calls, 1)
        self.assertEqual(result['shots'][0]['still_frame_status'], 'ready')
        self.assertEqual(result['shots'][0]['video_status'], 'processing')
        self.assertEqual(result['shots_needing_attention'], [])
        self.assertEqual(result['shots'][1], self.result['shots'][1])

    def test_old_video_never_bypasses_missing_preview_recovery(self):
        self.result['shots'][0]['video_url'] = 'https://audit/old-video.mp4'
        jobs.set_result(self.db, self.job.id, self.result)
        task = BackgroundTasks()
        with patch.object(video, 'start') as start:
            response = routes.regenerate_video(
                self.job.id, 1, routes.VideoRegenerateRequest(expected_attempt='none'), task, self.db)
        self.assertEqual(response['status'], 'generating_still')
        start.assert_not_called()

    def test_duplicate_claim_is_rejected(self):
        jobs.claim_still_retry(self.db,self.job.id,1,'none')
        with self.assertRaisesRegex(ValueError,'already regenerating'):
            jobs.claim_still_retry(self.db,self.job.id,1,'none')

    def test_visual_mismatch_allows_one_improved_manual_recovery_not_an_endless_loop(self):
        self.result['shots'][0]['still_frame_error_kind'] = 'mismatch'
        jobs.set_result(self.db, self.job.id, self.result)
        jobs.claim_still_retry(self.db, self.job.id, 1, 'none')
        saved = jobs.job_result(jobs.get_job(self.db, self.job.id))
        self.assertEqual(saved['shots'][0]['still_retry_count'], 1)
        stored = json.loads(jobs.get_job(self.db, self.job.id).result_json)
        shot = stored['shots'][0]
        shot.update(still_frame_status='failed', still_frame_error_kind='mismatch')
        shot.pop('still_retry_token', None); shot.pop('still_retry_started_at', None)
        self.job.result_json = json.dumps(stored); self.db.commit()
        with self.assertRaisesRegex(ValueError, 'already been tried'):
            jobs.claim_still_retry(self.db, self.job.id, 1, 'none')

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
        self.assertEqual(seen, ['pending', 'generating', 'ready'])

    def test_verification_outage_preserves_image_and_leaves_sibling_unchanged(self):
        def run(error=False):
            task = BackgroundTasks()
            with patch.object(routes, 'SessionLocal', self.sessions), \
                 patch.object(still, 'generate_still', return_value=self.image) as generate, \
                 patch.object(still, 'check_still', side_effect=still.VerificationUnavailable('offline') if error else None,
                              return_value={'approved':True,'reason':'Matches'}), \
                 patch.object(still, '_download_reference_image', return_value=self.image), \
                 patch.object(still.storage_service, 'upload_bytes', return_value={'key':'audit/candidate','url':'https://audit/still'}), \
                 patch.object(still.storage_service, 'asset_url', return_value='https://audit/candidate'), \
                 patch('app.services.preview_display.store_variants', return_value=None):
                routes.retry_preview(self.job.id, 1, routes.VideoRegenerateRequest(expected_attempt='none'), task, self.db)
                asyncio.run(task())
                self.db.expire_all()
                return jobs.job_result(jobs.get_job(self.db,self.job.id)), generate.call_count
        ready, calls = run(True)
        self.assertEqual(calls, 1)
        self.assertEqual(ready['shots'][0]['still_frame_status'], 'ready')
        self.assertEqual(ready['shots'][0]['still_frame_url'], 'https://audit/candidate')
        self.assertNotIn('still_frame_candidate', ready['shots'][0])
        self.assertEqual(ready['shots'][1], self.result['shots'][1])
        self.assertFalse(ready['shots'][0].get('video_url'))

    def test_stale_token_cannot_save(self):
        jobs.claim_still_retry(self.db,self.job.id,1,'none')
        self.assertFalse(jobs.finish_still_retry(self.db,self.job.id,1,'stale',self.result))

    def dependency_retry(self):
        from app.services.preview_plan import preview_input
        self.result['shots'][0].update(still_frame_url='https://audit/first', still_frame_key='first')
        target = self.result['shots'][1]
        target.update(description='Neighbor unchanged.', characters_in_shot=[], still_frame_url=None,
                      still_frame_status='failed', preview_dependencies={'1': None})
        target['preview_input'] = preview_input(self.result, target)
        self.job.result_json = json.dumps(self.result)
        self.db.commit()
        token = jobs.claim_still_retry(self.db, self.job.id, 2, 'none')
        rendered = copy.deepcopy(self.result)
        rendered['shots'][1].update(preview_dependencies={'1': 'first'}, still_frame_status='ready',
                                   still_frame_url='https://audit/second', still_frame_key='second')
        rendered['shots'][1]['still_frame_source_hash'] = still.shot_fingerprint(rendered['shots'][1])
        return token, rendered

    def test_recovered_upstream_dependency_does_not_discard_finished_retry(self):
        token, rendered = self.dependency_retry()
        self.assertTrue(jobs.finish_still_retry(self.db, self.job.id, 2, token, rendered))
        saved = json.loads(jobs.get_job(self.db, self.job.id).result_json)
        self.assertEqual(saved['shots'][1]['still_frame_status'], 'ready')
        self.assertEqual(saved['shots'][1]['preview_dependencies'], {'1': 'first'})
        self.assertEqual(still.shot_fingerprint(saved['shots'][1]), saved['shots'][1]['still_frame_source_hash'])
        self.assertEqual(saved['shots'][0], self.result['shots'][0])

    def test_upstream_changed_during_retry_is_rejected_with_stopped_state(self):
        token, rendered = self.dependency_retry()
        stored = json.loads(jobs.get_job(self.db, self.job.id).result_json)
        stored['shots'][0]['still_frame_key'] = 'changed-during-render'
        self.job.result_json = json.dumps(stored)
        self.db.commit()
        self.assertFalse(jobs.finish_still_retry(self.db, self.job.id, 2, token, rendered))
        saved = json.loads(jobs.get_job(self.db, self.job.id).result_json)['shots'][1]
        self.assertEqual(saved['still_frame_status'], 'failed')
        self.assertNotIn('still_retry_token', saved)
        self.assertFalse(saved.get('still_frame_url'))

    def test_plan_edited_during_retry_cannot_accept_old_image(self):
        token, rendered = self.dependency_retry()
        stored = json.loads(jobs.get_job(self.db, self.job.id).result_json)
        stored['shots'][1]['description'] = 'Different action after edit'
        self.job.result_json = json.dumps(stored)
        self.db.commit()
        self.assertFalse(jobs.finish_still_retry(self.db, self.job.id, 2, token, rendered))
        saved = json.loads(jobs.get_job(self.db, self.job.id).result_json)['shots'][1]
        self.assertEqual(saved['still_frame_status'], 'failed')
        self.assertEqual(saved['description'], 'Different action after edit')

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

    def test_completed_plan_can_retry_all_missing_failed_previews_once(self):
        self.result['assembly'] = {'provisional': False}
        self.result['shots'][0].update(still_frame_status='failed', still_frame_url=None,
            still_frame_error_kind='generation')
        self.job.result_json = json.dumps(self.result)
        self.job.status = 'done'
        self.db.commit()
        jobs.claim_preview_preparation(self.db, self.job.id)
        restored = jobs.job_result(jobs.get_job(self.db, self.job.id))
        self.assertTrue(restored['preview_preparation_pending'])
        self.assertTrue(restored['audio_assembly_pending'])

    def test_preparation_retry_preserves_existing_sibling_images(self):
        self.job.status = 'error'; self.db.commit()
        jobs.claim_preview_preparation(self.db, self.job.id)
        restored = jobs.job_result(jobs.get_job(self.db, self.job.id))
        self.assertEqual(restored['shots'][1]['still_frame_url'], 'https://audit/neighbor')

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

