import copy
import io
import json
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import VideoTask
from app.services import audio_video_service as audio, video_generation_service as video
from app.services import job_service as jobs, render_compliance_service as gate, hedra_video_service as hedra


class AudioVideoTests(unittest.TestCase):
    def setUp(self):
        # Media preflight has real WAV/MP3 and submission tests in its own suite.
        self.audio_preflight = patch('app.services.seedance_audio_reference.prepare', return_value=None)
        self.audio_preflight.start(); self.addCleanup(self.audio_preflight.stop)
        self.engine = create_engine('sqlite://'); Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.shot = dict(shot_number=1, has_dialogue=True, speech_mode='onscreen', speaker_label='Meera',
                         characters_in_shot=['Meera'], compiled_prompt='Meera reads outdoors, then looks up to speak.',
                         still_frame_url='https://example.com/scene.jpg', still_frame_status='ready',
                         dialogue_audio_url='https://example.com/approved.wav', dialogue_audio_duration_sec=4.2,
                         dialogue_text='Hello there.', duration_sec=4.2,
                         shot_direction={'action_beats':['Meera looks up and speaks the approved line.'],
                                         'dialogue_beat_index':1})
        self.result = dict(shots=[self.shot], generation_approved=True, quality='720p', aspect_ratio='16:9',
                           continuity={'characters': [{'name':'Meera','character_id':'vault', 'image_url':'https://example.com/portrait.jpg'}]})
        self.job = jobs.create_job(self.db, 'isolated audit', ai_model='Seedance 2.0')
        jobs.set_result(self.db,self.job.id,self.result); jobs.set_status(self.db,self.job.id,'done')
        for key in ('fal_api_key','evolink_api_key'):
            p=patch.object(video.settings,key,'test'); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose); self.addCleanup(self.db.close)

    def test_performance_time_survives_short_speech(self):
        self.shot.update(duration_sec=12, dialogue_audio_duration_sec=2.56)
        translated = audio.translate(self.result, self.shot, 'seedance_mini_evolink')
        self.assertEqual(translated['request']['duration'], 12)
        self.shot.update(duration_sec=0.85, dialogue_audio_duration_sec=0.85)
        self.assertEqual(audio.translate(self.result, self.shot, 'seedance_mini_evolink')['request']['duration'], 5)
        self.shot['duration_sec'] = 16
        with self.assertRaises(ValueError):
            audio.translate(self.result, self.shot, 'seedance_mini_evolink')

    def saved(self):
        self.db.expire_all()
        return {"shot_number": 1, **json.loads(self.db.query(VideoTask).one().data_json)}

    def test_scene_remains_first_with_seedance_identity_and_approved_audio(self):
        for model in audio.MODELS:
            with self.subTest(model=model):
                t=video.translate(self.result,self.shot,audio_model=model); r=t['request']
                if model.startswith('kling_'): self.assertNotIn('portrait',json.dumps(r))
                if model=='kling_avatar_fal':
                    self.assertEqual(r['image_url'],self.shot['still_frame_url'])
                    self.assertEqual(r['audio_url'],self.shot['dialogue_audio_url'])
                    self.assertNotIn('duration',r);self.assertNotIn('resolution',r)
                elif model=='h3_max_fal':
                    self.assertEqual(r['reference_image_urls'], [self.shot['still_frame_url'], 'https://example.com/portrait.jpg'])
                    self.assertEqual(r['reference_audio_urls'], [self.shot['dialogue_audio_url']])
                    self.assertEqual(r['resolution'], '768P')
                    self.assertEqual(r['duration'], 5)
                elif model=='kling_voice_fal':
                    self.assertEqual(r['start_image_url'],self.shot['still_frame_url'])
                    self.assertIn('Hello there.',r['prompt'])
                    self.assertNotIn('audio_urls',r)
                else:
                    self.assertEqual(r['image_urls'],[self.shot['still_frame_url'], 'https://example.com/portrait.jpg'])
                    self.assertEqual(r['audio_urls'],[self.shot['dialogue_audio_url']])
                    self.assertTrue(r['generate_audio'])
                self.assertNotIn('video_url',r);self.assertNotIn('video_urls',r)

    def test_missing_stale_and_long_audio_are_blocked(self):
        for update in ({'still_frame_url':None},{'still_frame_status':'failed'}, {'still_frame_source_hash':'stale'},
                       {'dialogue_audio_url':None},{'dialogue_audio_duration_sec':16}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                video.translate(self.result,{**self.shot,**update})

    def test_seedance_keeps_native_transcript_after_visual_sections_are_stripped(self):
        line = 'यह जोड़ कभी नहीं टूटेगा... अब चलते हैं आत्मा लेने।'
        shot = {**self.shot, 'speaker_label': 'Yamaraj', 'dialogue_text': line,
                'compiled_prompt': 'A workshop.\nDialogue: obsolete compiled wording'}
        for model in [m for m in audio.MODELS if m.startswith('seedance_')]:
            with self.subTest(model=model):
                request = video.translate({**self.result, 'language': 'Hindi', 'video_model': model}, shot)['request']
                self.assertIn(line, request['prompt'])
                self.assertIn('"speaker": "Yamaraj"', request['prompt'])
                self.assertIn('"language": "Hindi"', request['prompt'])
                self.assertNotIn('obsolete compiled wording', request['prompt'])
                self.assertEqual(request['audio_urls'], [shot['dialogue_audio_url']])
                self.assertEqual(request['image_urls'][0], shot['still_frame_url'])

    def test_seedance_missing_transcript_fails_before_provider_submission(self):
        for text in (None, '', '  '):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Approved dialogue text is missing'):
                video.translate(self.result, {**self.shot, 'dialogue_text': text})

    def test_mini_narration_and_saved_automatic_include_transcript(self):
        for model in ('seedance_mini_evolink', 'seedance_mini_fal', 'automatic_omni_mini'):
            with self.subTest(model=model):
                shot = {**self.shot, 'speech_mode': 'voiceover', 'characters_in_shot': [], 'speaker_label': None}
                result = {**self.result, 'ai_model': 'Seedance 2.0', 'language': 'English', 'video_model': model}
                request = video.translate(result, shot)['request']
                self.assertIn(self.shot['dialogue_text'], request['prompt'])
                self.assertIn('off-screen narrator', request['prompt'])
                self.assertIn('No visible person speaks', request['prompt'])
                self.assertNotIn("Synchronize the visible speaker", request['prompt'])

    def test_fal_durable_submission_and_duplicate_guard(self):
        with patch.object(audio,'fal_request',return_value={'request_id':'one','status_url':'https://queue.fal.run/status',
                          'response_url':'https://queue.fal.run/result'}) as call, patch.object(hedra,'api') as old:
            video.start(self.db,self.job.id,1,audio_model='kling_avatar_fal')
            with self.assertRaises(ValueError): video.start(self.db,self.job.id,1,audio_model='kling_avatar_fal')
            self.assertEqual(call.call_count,1);old.assert_not_called()
            data=self.saved();self.assertEqual(data['video_audio_model'],'kling_avatar_fal')
            self.assertEqual(data['video_fal_status_url'],'https://queue.fal.run/status')
            self.assertEqual(data['video_retry_request']['audio_url'],self.shot['dialogue_audio_url'])
            self.assertTrue(data['video_onscreen_speech'])
            self.assertNotIn('video_approved_audio_url',data)
            self.assertIn('start_sec', data['video_compliance_expected']['speech'])

    def test_fal_queue_failure_and_completed_media(self):
        shot={'video_fal_status_url':'https://queue.fal.run/status','video_fal_response_url':'https://queue.fal.run/result','video_model':'test'}
        with patch.object(audio,'fal_request',return_value={'status':'COMPLETED','error':'provider failed'}) as call:
            self.assertEqual(audio.poll(shot)['status'],'failed');self.assertEqual(call.call_count,1)
        with patch.object(audio,'fal_request',side_effect=[{'status':'COMPLETED','metrics':{'inference_time':42}}, {'video':{'url':'https://example.com/result.mp4'}}]):
            result=audio.poll(shot);self.assertEqual(result['results'],['https://example.com/result.mp4'])
            self.assertEqual(result['usage']['metrics']['inference_time'],42)

    def test_queue_credentials_never_follow_untrusted_urls(self):
        with patch.object(audio.httpx,'request') as call:
            for url in ('https://evil.example/status','http://queue.fal.run/status','https://queue.fal.run@evil.example/status'):
                with self.assertRaises(ValueError): audio.fal_request('GET',url)
            call.assert_not_called()

    def test_real_media_requires_decodable_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            voiced, silent = Path(folder)/'voiced.mp4', Path(folder)/'silent.mp4'
            subprocess.run([hedra.ffmpeg(), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'color=size=320x180:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-t', '0.4', '-c:v', 'libx264', '-c:a', 'aac', str(voiced)], check=True, timeout=30)
            subprocess.run([hedra.ffmpeg(), '-v', 'error', '-y', '-i', str(voiced), '-an',
                '-c:v', 'copy', str(silent)], check=True, timeout=30)
            with voiced.open('rb') as media:
                audio.validate_audio_result(media)
                self.assertFalse(media.closed)
                self.assertEqual(media.tell(), 0)
            with silent.open('rb') as media, self.assertRaisesRegex(ValueError, 'no usable audio/video'):
                audio.validate_audio_result(media)

    def test_fal_compliance_retry_uses_same_model_and_audio(self):
        response={'id':'one','status_url':'https://queue.fal.run/status','response_url':'https://queue.fal.run/result'}
        with patch.object(audio,'submit',return_value=response):
            video.start(self.db,self.job.id,1,audio_model='seedance_fal')
        request=self.saved()['video_retry_request']
        self.assertIn(self.shot['dialogue_text'], request['prompt'])
        mismatch={'verdict':{k:{'status':'mismatch' if k=='scale' else 'pass','observed':'wide','reason':'too wide'} for k in ('style','scale')}}
        with patch.object(gate,'inspect',return_value=mismatch), patch.object(audio,'submit',return_value={**response,'id':'two'}) as call, patch.object(hedra,'api') as old:
            self.assertFalse(gate.accept(self.db,self.job.id,self.saved(),io.BytesIO()))
            sent=call.call_args.args[1]
            self.assertEqual(call.call_args.args[0],'bytedance/seedance-2.0/reference-to-video')
            self.assertEqual(sent['audio_urls'],request['audio_urls'])
            self.assertEqual(sent['image_urls'],request['image_urls'])
            self.assertIn('Automatic corrective retry',sent['prompt'])
            old.assert_not_called();self.assertEqual(self.saved()['video_task_id'],'two')
            self.assertEqual(self.saved()['video_phase'],'correcting')
            self.assertFalse(gate.accept(self.db,self.job.id,self.saved(),io.BytesIO()))
            self.assertEqual(self.saved()['video_status'], 'review_required')
            self.assertEqual(call.call_count,1)

    def test_completed_fal_result_reuses_s3_pipeline(self):
        with patch.object(audio,'submit',return_value={'id':'one','status_url':'https://queue.fal.run/status','response_url':'https://queue.fal.run/result'}):
            video.start(self.db,self.job.id,1,audio_model='seedance_fal')
        download=MagicMock();download.__enter__.return_value.iter_bytes.return_value=[b'\0\0\0\x20ftypisom'+b'0'*40]
        with patch.object(audio,'poll',return_value={'status':'completed','model':self.saved()['video_model'],'results':['https://example.com/video.mp4']}), patch.object(video.httpx,'stream',return_value=download), patch.object(audio,'validate_audio_result'), patch('app.services.approved_audio_lock.apply',side_effect=AssertionError('post-generation audio replacement is forbidden')) as lock, patch.object(gate,'accept',return_value=True), patch.object(video.storage_service,'upload_file',return_value={'url':'https://example.com/stored.mp4'}):
            video.poll(self.db,self.job.id,self.saved())
        lock.assert_not_called()
        self.assertEqual(self.saved()['video_status'],'done')
        self.assertEqual(self.saved()['video_url'],'https://example.com/stored.mp4')


if __name__=='__main__':unittest.main()
