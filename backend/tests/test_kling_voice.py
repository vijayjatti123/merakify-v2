import copy
import json
import os
from unittest.mock import patch

from test_audio_video import AudioVideoTests
from app.services import audio_video_service as audio, kling_voice_service as voice, job_service as jobs, video_generation_service as video
from app.models import KlingVoice, VideoTask
from app.config import Settings


class KlingVoiceTests(AudioVideoTests):
    def prepare(self):
        self.shot.update(dialogue_audio_duration_sec=6.1, dialogue_voice_id="ritu", dialogue_audio_provider="sarvam")
        jobs.set_result(self.db, self.job.id, self.result)

    def receipt(self, task):
        return {"status":"IN_QUEUE","request_id":task,"status_url":"https://queue.fal.run/"+task+"/status", "response_url":"https://queue.fal.run/"+task}

    def test_voice_setup_render_and_reuse(self):
        self.prepare()
        with patch.object(audio, "fal_request", return_value=self.receipt("voice-task")) as api:
            video.start(self.db,self.job.id,1,audio_model="kling_voice_fal")
            self.assertEqual(api.call_count,1)
            self.assertTrue(api.call_args.args[1].endswith("/create-voice"))
            self.assertEqual(api.call_args.args[2], {"voice_url":self.shot["dialogue_audio_url"]})
        pending=self.saved()
        self.assertEqual(pending["video_phase"],"preparing_voice")
        with patch.object(audio,"fal_request",side_effect=[{"status":"COMPLETED"},{"voice_id":"fal-voice-123"},self.receipt("scene-task")]) as api:
            video.poll(self.db,self.job.id,pending)
            self.assertEqual(api.call_count,3)
            request=api.call_args.args[2]
            self.assertEqual(request["elements"][0]["voice_id"],"fal-voice-123")
            self.assertEqual(request["start_image_url"],self.shot["still_frame_url"])
        ready=self.saved()
        self.assertEqual(ready["video_task_id"],"scene-task")
        self.assertEqual(ready["video_retry_request"]["elements"][0]["voice_id"],"fal-voice-123")
        # A stale second worker must not submit the scene twice.
        with patch.object(audio,"fal_request") as api:
            video.poll(self.db,self.job.id,pending)
            api.assert_not_called()
        jobs.update_video(self.db,self.job.id,1,video_status="done")
        # The same character can now use short lines without cloning again.
        self.shot["dialogue_audio_duration_sec"]=1.7
        jobs.set_result(self.db,self.job.id,self.result)
        with patch.object(audio,"fal_request",return_value=self.receipt("second-scene")) as api:
            video.start(self.db,self.job.id,1,regenerate=True,expected_attempt="scene-task",audio_model="kling_voice_fal")
            api.assert_not_called()
            video.poll(self.db,self.job.id,self.saved())
            self.assertEqual(api.call_count,1)
            self.assertNotIn("create-voice",api.call_args.args[1])

    def test_short_initial_sample_blocks_before_any_paid_request(self):
        self.prepare();self.shot["dialogue_audio_duration_sec"]=1.7
        jobs.set_result(self.db,self.job.id,self.result)
        with patch.object(audio,"fal_request") as api, self.assertRaisesRegex(ValueError,"5–30"):
            video.start(self.db,self.job.id,1,audio_model="kling_voice_fal")
        api.assert_not_called();self.assertEqual(self.db.query(VideoTask).count(),0)

    def test_voice_failure_never_submits_scene(self):
        self.prepare()
        with patch.object(audio,"fal_request",return_value=self.receipt("voice-task")):
            video.start(self.db,self.job.id,1,audio_model="kling_voice_fal")
        with patch.object(audio,"fal_request",return_value={"status":"FAILED"}) as api:
            video.poll(self.db,self.job.id,self.saved())
            self.assertEqual(api.call_count,1)
        self.assertEqual(self.saved()["video_status"],"review_required")
        self.assertIn("No scene render",self.saved()["video_error"])

    def test_uncertain_voice_submission_is_not_repeated(self):
        self.prepare()
        with patch.object(audio,"fal_request",side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):video.start(self.db,self.job.id,1,audio_model="kling_voice_fal")
        self.assertEqual(self.db.query(KlingVoice).one().status,"submission_unknown")
        with self.assertRaisesRegex(ValueError,"reconciliation"):
            voice.preparation(self.db,self.job.id,self.result,self.shot)

    def test_changed_voice_does_not_reuse_old_identity(self):
        self.prepare();key,sample=voice.preparation(self.db,self.job.id,self.result,self.shot)
        jobs.claim_kling_voice(self.db,key,{})
        jobs.update_kling_voice(self.db,key,status="ready",voice_id="old")
        changed={**self.shot,"dialogue_voice_id":"priya"}
        new_key,new_sample=voice.preparation(self.db,self.job.id,self.result,changed)
        self.assertNotEqual(key,new_key);self.assertIsNotNone(new_sample)

    def test_hindi_from_authoritative_job_is_blocked(self):
        self.prepare();self.job.language="Hindi";self.db.commit()
        with patch.object(audio,"fal_request") as api, self.assertRaisesRegex(ValueError,"translate"):
            video.start(self.db,self.job.id,1,audio_model="kling_voice_fal")
        api.assert_not_called()

    def test_fast_mini_limits_for_both_providers(self):
        for model in ("seedance_fast_fal","seedance_mini_fal","seedance_fast_evolink","seedance_mini_evolink"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError,"resolution"):
                    audio.translate({**self.result,"quality":"1080p"},self.shot,model)
                payload=audio.translate(self.result,self.shot,model)["request"]
                self.assertEqual(payload["audio_urls"],[self.shot["dialogue_audio_url"]])

    def test_hyphenated_env_alias(self):
        with patch.dict(os.environ,{"FAL_API-KEY":"test-alias"},clear=True):
            self.assertEqual(Settings(_env_file=None).fal_api_key,"test-alias")
        with patch.dict(os.environ,{"FAL_API-KEY":"old","FAL_API_KEY":"preferred"},clear=True):
            self.assertEqual(Settings(_env_file=None).fal_api_key,"preferred")
