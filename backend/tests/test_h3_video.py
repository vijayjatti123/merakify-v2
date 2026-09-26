import unittest
from unittest.mock import patch, MagicMock
from app.schemas import JobCreate
from app.services import video_generation_service as video, audio_video_service as audio, job_service as jobs
from app.services import seedance_audio_reference as refs
from app.services.shot_prompt_compiler import model_family
import test_audio_video
from test_seedance_audio_reference import wav


class H3Tests(unittest.TestCase):
    def setUp(self):
        test_audio_video.AudioVideoTests.setUp(self)
        self.job.video_model = "h3_max_fal"
        self.job.ai_model = "MiniMax H3 Max"
        self.db.commit()
        guard = patch.object(video, "provider", side_effect=AssertionError("Unexpected EvoLink call"))
        guard.start(); self.addCleanup(guard.stop)
        self.result.update(video_model="h3_max_fal", ai_model="MiniMax H3 Max")
        jobs.set_result(self.db, self.job.id, self.result)

    def test_new_default_and_explicit_mini(self):
        fresh = JobCreate(brief="A simple ad")
        self.assertEqual((fresh.ai_model, fresh.video_model), ("MiniMax H3 Max", "h3_max_fal"))
        old = JobCreate(brief="A simple ad", ai_model="Seedance 2.0", video_model="seedance_mini_evolink")
        self.assertEqual(old.video_model, "seedance_mini_evolink")
        self.assertEqual(model_family(fresh.ai_model), "generic")

    def test_all_speech_modes_use_correct_reference_contract(self):
        for mode in ("none", "voiceover", "onscreen"):
            shot = {**self.shot, "has_dialogue": mode != "none", "speech_mode": mode}
            t = video.translate(self.result, shot)
            r = t["request"]
            self.assertEqual(t["model"], "minimax/h3-max/reference-to-video" if mode == "none" else
                             "minimax/h3-max/image-to-video")
            self.assertEqual(r["prompt_expansion_mode"], "disabled")
            self.assertEqual(r["duration"], 5)
            self.assertEqual(r["resolution"], "768P")
            if mode == "none":
                self.assertEqual(r["reference_image_urls"][:2],
                                 [shot["still_frame_url"], "https://example.com/portrait.jpg"])
                self.assertIn("Image 1:", r["prompt"])
                self.assertNotIn("target_audio_url", r)
            else:
                self.assertEqual(r["image_url"], shot["still_frame_url"])
                self.assertEqual(r["target_audio_url"], shot["dialogue_audio_url"])
                self.assertNotIn("reference_image_urls", r)
                self.assertNotIn("reference_audio_urls", r)
                self.assertIn('"Hello there."', r["prompt"])
                self.assertIn("PERFORMANCE TIMELINE", r["prompt"])
                self.assertIn("only authority for action order and speech", r["prompt"])
                self.assertNotIn("then looks up to speak", r["prompt"])
                self.assertIn("only audio authority", r["prompt"])
                self.assertIn("everyone is silent", r["prompt"])
            if mode == "voiceover":
                self.assertIn("No visible person moves their mouth", r["prompt"])

    def test_product_only_voiceover_does_not_invite_other_story_characters(self):
        self.result['continuity']['characters'].append({'name':'Yamaraj'})
        self.result['continuity']['visual_style'] = {
            'rendering':'Natural live action, lifelike skin/fabric textures',
            'palette':'Warm wood browns, soft skin tones, muted whites',
            'lighting_motif':'Workshop window light, golden glow emanating from Yamaraj and the bucket',
        }
        shot = {**self.shot, 'characters_in_shot':[], 'opening_characters':[],
                'speech_mode':'voiceover', 'state_at_shot_start':'Bucket alone on workbench.',
                'state_at_shot_end':'Bucket alone fills the frame.',
                'shot_direction':{'action_beats':['Camera moves toward bucket.', 'Narrator delivers line.', 'Hold on bucket.'],
                                  'dialogue_beat_index':2, 'forbidden_geometry':['Any human hands or characters entering frame']}}
        prompt = video.translate(self.result, shot)['request']['prompt']
        for cue in ('Yamaraj', 'skin tones', 'people, setting', 'wardrobe', 'mouths', 'characters entering frame'):
            self.assertNotIn(cue, prompt)
        self.assertIn('Bucket alone on workbench', prompt)
        self.assertIn('product-only composition', prompt)
        self.assertIn('"Hello there."', prompt)

    def test_duration_and_missing_inputs_fail_before_submission(self):
        for change in ({"duration_sec": 16}, {"duration_sec": float("nan")},
                       {"still_frame_status": "failed"}, {"still_frame_url": None},
                       {"dialogue_audio_url": None}, {"dialogue_text": ""},
                       {"dialogue_audio_duration_sec": 16}, {"still_frame_source_hash": "stale"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                video.translate(self.result, {**self.shot, **change})
        self.assertEqual(video.translate(self.result, {**self.shot, "duration_sec": 12})["request"]["duration"], 12)
        self.assertEqual(video.translate({**self.result,"quality":"480p"}, self.shot)["request"]["resolution"], "480P")

    def test_full_regeneration_never_uses_seedance_edit_contract(self):
        shot = {**self.shot, "has_dialogue": False, "speech_mode": "none", "video_key": "old.mp4"}
        t = video.regenerate_translation(self.result, shot, "Warmer lighting")
        self.assertNotIn("video_urls", t["request"])
        self.assertIn("Warmer lighting", t["request"]["prompt"])

    def test_silent_shot_keeps_directed_action_order_without_inventing_speech(self):
        shot = {**self.shot, "has_dialogue": False, "speech_mode": "none",
                "description": "Kabir reaches for the bottle.",
                "shot_direction": {"action_beats": ["Kabir hesitates.", "Kabir reaches for the bottle."]}}
        request = video.translate(self.result, shot)["request"]
        prompt = request["prompt"]
        self.assertLess(prompt.index("Kabir hesitates."), prompt.rindex("Kabir reaches for the bottle."))
        self.assertIn("no dialogue, muttering", prompt)
        self.assertNotIn("target_audio_url", request)

    def test_short_audio_preflight_and_durable_request(self):
        self.audio_preflight.stop()
        self.shot["dialogue_audio_duration_sec"] = 1
        jobs.set_result(self.db, self.job.id, self.result)
        download = MagicMock()
        download.__enter__.return_value.iter_bytes.return_value = [wav(1)]
        with patch.object(refs.httpx, "stream", return_value=download), patch.object(refs.storage_service, "upload_bytes", return_value={"key":"padded.wav"}), patch.object(refs.storage_service, "asset_url", return_value="https://example.com/padded.wav"), patch.object(audio,"submit",return_value={"id":"h3-task","status_url":"https://queue.fal.run/status","response_url":"https://queue.fal.run/result"}) as submit:
            video.start(self.db, self.job.id, 1)
            r = submit.call_args.args[1]
            self.assertEqual(r["target_audio_url"], "https://example.com/padded.wav")
            saved = jobs.video_source(self.db, self.job.id, 1)[1]
            self.assertEqual(saved["video_provider"], "fal")
            self.assertEqual(saved["video_audio_model"], "h3_max_fal")
            self.assertEqual(saved["video_retry_request"], r)
            self.assertEqual(saved["dialogue_audio_url"], self.shot["dialogue_audio_url"])
            with self.assertRaises(ValueError):
                video.start(self.db, self.job.id, 1)
            self.assertEqual(submit.call_count, 1)

    def test_reference_tags_and_combined_limit(self):
        from app.services import video_references
        with self.assertRaisesRegex(ValueError, "unbound"):
            video_references.check_prompt("Image 9: unknown", [{"tag":"Image 1"}])
        self.result["continuity"]["characters"][0]["reference_sheet_url"] = "https://example.com/views.jpg"
        r = video.translate(self.result, {**self.shot, "has_dialogue": False, "speech_mode": "none"})["request"]
        self.assertEqual(len(r["reference_image_urls"]), 3)
        self.assertIn("Image 3", r["prompt"])

    def test_overrides_rejected(self):
        with self.assertRaises(ValueError):
            video.translate(self.result, self.shot, audio_model="seedance_mini_evolink")

    def test_default_intake_persistence_and_retry(self):
        from fastapi import BackgroundTasks
        from app.routes import jobs as routes
        out = routes.create_job(JobCreate(brief="A lamp glows on a desk"), BackgroundTasks(), self.db)
        self.db.expire_all()
        restored = routes.get_job(out.id, self.db)
        self.assertEqual(restored.video_model, "h3_max_fal")
        copied = jobs.copy_job_for_retry(self.db, jobs.get_job(self.db, out.id))
        self.assertEqual((copied.ai_model, copied.video_model), ("MiniMax H3 Max", "h3_max_fal"))

    def test_fal_poll_preserves_distinct_provider_timings(self):
        with patch.object(audio, "fal_request", side_effect=[
            {"status":"COMPLETED","metrics":{"inference_time":15}},
            {"video":{"url":"https://example.com/video.mp4"}, "timings":{"inference":5}}]):
            outcome = audio.poll({"video_fal_status_url":"https://queue.fal.run/status",
                                  "video_fal_response_url":"https://queue.fal.run/result",
                                  "video_model":"minimax/h3-max/reference-to-video"})
        self.assertEqual(outcome["usage"]["timings"], {"inference":5})
        self.assertEqual(outcome["usage"]["metrics"], {"inference_time":15})

    def test_exact_audio_endpoint_is_accepted_by_durable_fal_submission(self):
        with patch.object(audio, "fal_request", return_value={
                "request_id": "one", "status_url": "https://queue.fal.run/status",
                "response_url": "https://queue.fal.run/result"}) as call:
            translated = video.translate(self.result, self.shot)
            task = audio.submit(translated["model"], translated["request"])
        self.assertEqual(task["id"], "one")
        self.assertEqual(call.call_args.args[1], "https://queue.fal.run/minimax/h3-max/image-to-video")
