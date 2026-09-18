import copy
import unittest
from unittest.mock import patch

from app.schemas import JobCreate
from app.video_models import AUTOMATIC, OMNI_FLASH
from app.services import video_generation_service as video, audio_video_service as audio


class AutomaticVideoTests(unittest.TestCase):
    def setUp(self):
        self.result = dict(ai_model='Seedance 2.0', video_model=AUTOMATIC, quality='720p', aspect_ratio='16:9')
        self.shot = dict(shot_number=1, duration_sec=5, has_dialogue=False, characters_in_shot=[],
                         compiled_prompt='A desk lamp turns on.', still_frame_url='https://example.com/scene.jpg',
                         still_frame_status='ready')

    def test_api_accepts_policy_and_rejects_wrong_quality_or_family(self):
        self.assertEqual(JobCreate(brief='Lamp ad', **self.result).video_model, AUTOMATIC)
        for change in [dict(quality='480p'), dict(ai_model='Kling 3.0')]:
            with self.assertRaises(ValueError): JobCreate(brief='Lamp ad', **{**self.result, **change})

    def test_silent_uses_exact_preview_and_supported_omni_schema(self):
        result, shot = copy.deepcopy(self.result), copy.deepcopy(self.shot)
        translated = video.translate(result, shot)
        self.assertEqual(translated['model'], OMNI_FLASH)
        self.assertEqual(translated['provider'], 'fal')
        self.assertEqual(set(translated['request']), {'image_url', 'prompt', 'duration', 'aspect_ratio'})
        self.assertEqual(translated['request']['image_url'], shot['still_frame_url'])
        self.assertEqual(result, self.result)
        self.assertEqual(shot, self.shot)
        with patch.object(audio, 'fal_request', return_value={'request_id':'task', 'status_url':'https://queue.fal.run/status', 'response_url':'https://queue.fal.run/result'}) as request:
            self.assertEqual(audio.submit(translated['model'], translated['request'])['id'], 'task')
            self.assertEqual(request.call_args.args[1], 'https://queue.fal.run/'+OMNI_FLASH)

    def test_both_speech_modes_include_audio_reference_on_mini(self):
        for mode in ['onscreen', 'voiceover']:
            with self.subTest(mode=mode):
                shot = {**self.shot, 'has_dialogue':True, 'speech_mode':mode, 'speaker_label':'Meera',
                        'characters_in_shot':['Meera'], 'dialogue_audio_url':'https://example.com/speech.wav',
                        'dialogue_audio_duration_sec':4.5, 'dialogue_text':'A warmer home.'}
                translated = video.translate(self.result, shot)
                self.assertEqual(translated['model'], 'seedance-2.0-mini-reference-to-video')
                self.assertEqual(translated['provider'], 'evolink')
                self.assertEqual(translated['request']['audio_urls'], [shot['dialogue_audio_url']])
                self.assertTrue(translated['request']['generate_audio'])
                if mode == 'voiceover':
                    self.assertIn('No visible person speaks', translated['request']['prompt'])
                    self.assertNotIn('Synchronize the visible speaker', translated['request']['prompt'])

    def test_missing_speech_does_not_fall_back_to_silent_route(self):
        with self.assertRaises(ValueError):
            video.translate(self.result, {**self.shot, 'has_dialogue':True, 'speech_mode':'voiceover'})

    def test_stale_preview_and_unsupported_duration_fail_before_provider(self):
        for change in [dict(duration_sec=11), dict(duration_sec=float('nan')), dict(still_frame_status='failed'), dict(still_frame_source_hash='stale')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                video.translate(self.result, {**self.shot, **change})

    def test_regenerate_never_sends_unsupported_video_edit_input(self):
        translated = video.regenerate_translation(self.result, {**self.shot, 'video_key':'old.mp4'}, 'More gentle motion')
        self.assertNotIn('video_urls', translated['request'])
        self.assertIn('More gentle motion', translated['request']['prompt'])
        self.assertEqual(translated['model'], OMNI_FLASH)

    def test_existing_explicit_model_still_wins(self):
        translated = video.translate({**self.result, 'video_model':'seedance_evolink'}, self.shot)
        self.assertEqual(translated['model'], 'seedance-2.0-reference-to-video')


if __name__ == '__main__': unittest.main()
