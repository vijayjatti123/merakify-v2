import copy
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image
import test_module_f
from app.services import job_service as jobs, preview_replacement as replacement, still_frame_service as still
from app.services import video_generation_service as video, final_assembly_service as assembly
from app.models import FinalAssembly


def png(size=(640, 480)):
    out = io.BytesIO(); Image.new('RGB', size, '#805533').save(out, format='PNG'); return out.getvalue()


class PreviewReplacementTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)
        self.storage = patch('app.services.storage_service.asset_url', side_effect=lambda key: 'https://example.invalid/' + key)
        self.storage.start()
        self.background = patch('app.services.preview_replacement.SessionLocal', self.sessions)
        self.background.start()
        with self.sessions() as db:
            result = copy.deepcopy(self.original)
            for shot in result['shots']:
                shot.update(compiled_prompt='A quiet scene.', still_frame_key=f"old-{shot['shot_number']}.png",
                            still_frame_url=f"https://example.invalid/old-{shot['shot_number']}.png", still_frame_status='ready')
                shot['still_frame_source_hash'] = still.shot_fingerprint(shot)
            jobs.set_result(db, self.job_id, result)
        self.base = f'/api/jobs/{self.job_id}/shots/2/preview'

    def tearDown(self):
        self.background.stop(); self.storage.stop(); test_module_f.ModuleFTests.tearDown(self)

    def result(self):
        return self.client.get(f'/api/jobs/{self.job_id}').json()['result']

    def test_real_api_regenerate_review_accept_preserves_audio_siblings_and_marks_video_final_stale(self):
        original = self.result()
        def generate(result, **kwargs):
            self.assertEqual(kwargs['shot_numbers'], {2})
            self.assertEqual(kwargs['feedback_by_shot'], {2: 'Softer light'})
            shot = result['shots'][1]; shot.update(still_frame_key='new.png', still_frame_url='https://example.invalid/new.png')
        with self.sessions() as db:
            for shot in original['shots']:
                jobs.claim_video(db, self.job_id, shot['shot_number'], {'video_status': 'done', 'video_key': f"v{shot['shot_number']}", 'video_source_hash': video.source_fingerprint(shot)})
            job = jobs.get_job(db, self.job_id)
            source = assembly.fingerprint(jobs.job_result(job), job.aspect_ratio, job.quality, job.color_grade)
            db.add(FinalAssembly(job_id=self.job_id, status='done', data_json=json.dumps({'status': 'done', 'source_hash': source, 'key': 'final.mp4'})))
            db.commit()
        before = self.result()
        with patch.object(still, 'generate_still_frames', side_effect=generate):
            response = self.client.post(self.base + '/replacement', json={'expected_key': 'old-2.png', 'hint': 'Softer light'})
        self.assertEqual(response.status_code, 202, response.text)
        staged = self.result()
        self.assertEqual(staged['shots'][1]['still_frame_key'], 'old-2.png')
        self.assertFalse(staged['shots'][1]['video_source_changed'])
        token = staged['shots'][1]['preview_replacement']['token']
        response = self.client.post(self.base + '/decision', json={'token': token, 'accept': True})
        self.assertEqual(response.status_code, 200, response.text)
        final = self.result()
        self.assertEqual(final['shots'][1]['still_frame_key'], 'new.png')
        self.assertTrue(final['shots'][1]['video_source_changed'])
        self.assertTrue(final['final_video']['stale'])
        self.assertEqual(final['shots'][1]['dialogue_audio_url'], before['shots'][1]['dialogue_audio_url'])
        for index in (0, 2): self.assertEqual(final['shots'][index], before['shots'][index])

    def test_failed_generation_keeps_current(self):
        with patch.object(still, 'generate_still_frames', side_effect=RuntimeError('provider unavailable')):
            response = self.client.post(self.base + '/replacement', json={'expected_key': 'old-2.png'})
        self.assertEqual(response.status_code, 202)
        shot = self.result()['shots'][1]
        self.assertEqual(shot['still_frame_key'], 'old-2.png')
        self.assertEqual(shot['preview_replacement']['status'], 'failed')
        self.assertIn('unchanged', shot['preview_replacement']['warning'])

    def test_discard_and_stale_completion_cannot_replace_current(self):
        with self.sessions() as db:
            token, _ = replacement.claim(db, self.job_id, 2, 'old-2.png', 'generated')
            with self.assertRaisesRegex(ValueError, 'Review or discard'): replacement.claim(db, self.job_id, 2, 'old-2.png', 'generated')
            replacement.decide(db, self.job_id, 2, token, False)
            self.assertFalse(replacement.finish(db, self.job_id, 2, token, {'status': 'ready', 'key': 'late.png'}))
        self.assertEqual(self.result()['shots'][1]['still_frame_key'], 'old-2.png')

    def test_changed_plan_blocks_candidate_acceptance(self):
        with self.sessions() as db:
            token, _ = replacement.claim(db, self.job_id, 2, 'old-2.png', 'generated')
            replacement.finish(db, self.job_id, 2, token, {'status': 'ready', 'key': 'new.png', 'url': 'https://example.invalid/new.png'})
            jobs.update_shot_fields(db, self.job_id, 2, description='A different action')
            with self.assertRaisesRegex(ValueError, 'shot changed'): replacement.decide(db, self.job_id, 2, token, True)

    def test_upload_actual_multipart_validation_warning_requires_acknowledgement(self):
        with patch.object(still, 'check_still', return_value={'approved': False, 'reason': 'Different framing'}), patch('app.services.storage_service.upload_bytes', side_effect=lambda **kw: {'key': kw['key'], 'url': 'https://example.invalid/upload.png'}):
            response = self.client.post(self.base + '/upload', data={'expected_key': 'old-2.png', 'fit': 'fit'}, files={'file': ('test.png', png(), 'image/png')})
        self.assertEqual(response.status_code, 202, response.text)
        shot = self.result()['shots'][1]; candidate = shot['preview_replacement']
        self.assertIn('Different framing', candidate['warning'])
        self.assertEqual(shot['still_frame_key'], 'old-2.png')
        response = self.client.post(self.base + '/decision', json={'token': candidate['token'], 'accept': True})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(self.base + '/decision', json={'token': candidate['token'], 'accept': True, 'acknowledge': True})
        self.assertEqual(response.status_code, 200)

    def test_upload_validation_and_crop_fit_dimensions(self):
        for fit in ('crop', 'fit'):
            result = replacement.normalize_upload(png(), '16:9', fit)
            self.assertEqual(Image.open(io.BytesIO(result)).size, (1280, 720))
        for data in (b'not an image', png((100, 100)), b'x' * (replacement.MAX_BYTES + 1)):
            with self.assertRaises(ValueError): replacement.normalize_upload(data, '16:9', 'crop')
        self.assertEqual(Image.open(io.BytesIO(replacement.normalize_upload(png((1920, 1080)), '16:9', 'crop'))).size, (1920, 1080))
        with patch('app.services.preview_replacement.run') as run:
            response = self.client.post(self.base + '/upload', files={'file': ('bad.png', b'bad', 'image/png')})
        self.assertEqual(response.status_code, 409); run.assert_not_called()

    def test_busy_video_blocks_replacement(self):
        with self.sessions() as db: jobs.claim_video(db, self.job_id, 2, {'video_status': 'processing'})
        response = self.client.post(self.base + '/replacement', json={'expected_key': 'old-2.png'})
        self.assertEqual(response.status_code, 409)

    def test_expired_worker_is_visible_and_late_result_ignored(self):
        with self.sessions() as db:
            token, _ = replacement.claim(db, self.job_id, 2, 'old-2.png', 'generated')
            result = jobs.job_result(jobs.get_job(db, self.job_id))
            result['shots'][1]['preview_replacement']['started_at'] = 0
            jobs.set_result(db, self.job_id, result)
            self.assertFalse(replacement.finish(db, self.job_id, 2, token, {'status': 'ready', 'key': 'late.png'}))
        shot = self.result()['shots'][1]
        self.assertEqual(shot['preview_replacement']['status'], 'failed')
        self.assertEqual(shot['still_frame_key'], 'old-2.png')


if __name__ == '__main__': unittest.main()
