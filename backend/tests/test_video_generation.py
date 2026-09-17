import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import VideoTask
from app.services import job_service as jobs, video_generation_service as video


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.key_patch = patch.object(video.settings, "evolink_api_key", "test")
        self.key_patch.start(); self.addCleanup(self.key_patch.stop)
        self.shot = dict(shot_number=1, duration_sec=4, compiled_prompt="A cup, the stream still entering.",
                         still_frame_url="https://example.com/still.jpg", has_dialogue=False, characters_in_shot=[])
        self.result = dict(ai_model="Seedance 2.0", quality="480p", shots=[self.shot], continuity={})

    def test_real_map_shape_dedup_and_audio(self):
        self.result.update(continuity={"props": [{"name": "Cup"}, {"name": "Kettle"}]}, entity_references={
            "props:cup": {"url": "https://example.com/still.jpg?new=1"},
            "props:kettle": {"url": "https://example.com/unneeded.jpg"}})
        out = video.translate(self.result, self.shot)
        self.assertEqual(len(out['request']['image_urls']), 1)
        self.assertIn('job entity props:cup: @image1', out['request']['prompt'])
        self.assertIn('still entering', out['mode_risk_terms'])
        self.assertTrue(out['request']['generate_audio'])
        self.shot.update(has_dialogue=True,speech_mode="onscreen", dialogue_audio_duration_sec=4, dialogue_audio_url="https://example.com/real.wav")
        self.assertEqual(video.translate(self.result, self.shot)['provider'], 'evolink')

    def test_reference_priority_cap_and_url_rewrite(self):
        chars = [dict(name=f"Actor{n}", character_id=str(n), image_url=f"https://example.com/{n}.jpg") for n in range(10)]
        self.result['continuity']['characters'] = chars
        self.shot['characters_in_shot'] = [c['name'] for c in chars]
        self.shot['compiled_prompt'] += ' Maintain visual consistency with these reference images — Actor0: https://example.com/0.jpg. No on-screen text, logos or readable signage; composite text in post.'
        out = video.translate(self.result, self.shot)
        self.assertEqual(len(out['request']['image_urls']), 9)
        self.assertNotIn('https://', out['request']['prompt'])
        self.assertIn('Actor0: @image2', out['request']['prompt'])
        self.assertTrue(any('Actor8' in w for w in out['warnings']))
        self.assertTrue(any('Actor9' in w for w in out['warnings']))

    def test_constraints_and_duration_gates(self):
        self.shot['compiled_prompt'] += ' No photorealism. No extra hands.'
        out = video.translate(self.result, self.shot)
        self.assertIn('No photorealism', out['constraints'])
        self.assertEqual(out['request']['prompt'].count('Constraints:'), 1)
        self.shot['duration_sec'] = 1
        self.assertEqual(video.translate(self.result, self.shot)['request']['duration'], 4)
        for value in [16, float('nan'), 0]:
            self.shot['duration_sec'] = value
            with self.assertRaises(ValueError): video.translate(self.result, self.shot)

    @patch.object(video, 'provider')
    def test_durable_no_double_charge_and_replanning(self, provider):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            job = jobs.create_job(db, 'Video test', ai_model='Seedance 2.0', quality='480p')
            jobs.set_result(db, job.id, self.result)
            jobs.set_status(db, job.id, 'done')
            provider.return_value = {'id': 'test-task', 'model': video.MODEL, 'status': 'pending'}
            video.start(db, job.id, 1)
            with self.assertRaises(ValueError): video.start(db, job.id, 1)
            self.assertEqual(provider.call_count, 1)
            saved = jobs.pending_videos(db)[0][1]
            self.assertEqual(saved['video_task_id'], 'test-task')
            revised = copy.deepcopy(self.result)
            revised['shots'][0]['compiled_prompt'] = 'A changed plan.'
            jobs.set_result(db, job.id, revised)
            self.assertTrue(jobs.job_result(job)['shots'][0]['video_source_changed'])
            provider.return_value = {'status': 'completed', 'model': video.MODEL, 'results': ['https://example.com/video.mp4']}
            data = b'\0\0\0\x20ftypisom' + b'0' * 40
            download = MagicMock()
            download.__enter__.return_value.iter_bytes.return_value = [data]
            with patch.object(video.httpx, 'stream', return_value=download), patch.object(video.storage_service, 'upload_file', side_effect=RuntimeError('storage outage')):
                with self.assertRaises(RuntimeError): video.poll(db, job.id, saved)
            self.assertEqual(jobs.pending_videos(db)[0][1]['video_task_id'], 'test-task')
            with patch.object(video.httpx, 'stream', return_value=download), patch.object(video.storage_service, 'upload_file', return_value={'url': 'https://example.com/persisted.mp4'}):
                video.poll(db, job.id, saved)
            row = db.query(VideoTask).one()
            self.assertEqual(row.status, 'done')
            self.assertEqual(json.loads(row.data_json)['video_bytes'], len(data))
        engine.dispose()

    @patch.object(video, 'provider', return_value={'id': 'saved-task', 'model': video.MODEL})
    def test_trace_failure_does_not_lose_saved_task(self, provider):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            job = jobs.create_job(db, 'Trace recovery test', ai_model='Seedance 2.0', quality='480p')
            jobs.set_result(db, job.id, self.result)
            jobs.set_status(db, job.id, 'done')
            def trace(*args):
                if 'submitted; polling' in args[-1]:
                    raise RuntimeError('trace unavailable')
            with patch.object(jobs, 'append_event', side_effect=trace):
                with self.assertRaises(RuntimeError): video.start(db, job.id, 1)
            saved = jobs.pending_videos(db)[0][1]
            self.assertEqual(saved['video_status'], 'processing')
            self.assertEqual(saved['video_task_id'], 'saved-task')
            self.assertEqual(provider.call_count, 1)
        engine.dispose()

    def test_mode_mismatch_stops_and_crash_does_not_resubmit(self):
        shot = dict(shot_number=1, video_status='processing', video_task_id='id', video_submitted_at=datetime.now(timezone.utc).isoformat())
        with patch.object(video, 'provider', return_value={'model': 'video-edit'}), patch.object(jobs, 'update_video') as update, patch.object(jobs, 'append_event'):
            video.poll(None, 'job', shot)
            self.assertEqual(update.call_args.kwargs['video_status'], 'review_required')


if __name__ == '__main__':
    unittest.main()
