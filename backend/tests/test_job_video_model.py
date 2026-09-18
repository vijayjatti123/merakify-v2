import copy
import json
import unittest
from unittest.mock import patch
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from fastapi import BackgroundTasks
from app.db import Base
from app import db as database
from app.schemas import JobCreate
from app.routes import jobs as routes
from app.services import job_service as jobs, video_generation_service as video


class JobVideoModelTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        self.addCleanup(self.engine.dispose);self.addCleanup(self.db.close)
        self.shot=dict(shot_number=1,compiled_prompt="A cup sits on a desk.",duration_sec=5,
                       still_frame_url="https://example.com/still.png",has_dialogue=False,characters_in_shot=[])

    def test_real_intake_save_reload_retry_and_request_inherit(self):
        payload=JobCreate(brief="A lamp on a desk",ai_model="Seedance 2.0",video_model="seedance_mini_fal")
        out=routes.create_job(payload,BackgroundTasks(),self.db)
        self.assertEqual(out.video_model,"seedance_mini_fal")
        jobs.set_result(self.db,out.id,{"shots":[self.shot],"generation_approved":True})
        jobs.set_status(self.db,out.id,"done")
        self.db.expire_all()
        restored=routes.get_job(out.id,self.db)
        self.assertEqual(restored.video_model,"seedance_mini_fal")
        self.assertEqual(restored.result["video_model"],"seedance_mini_fal")
        copied=jobs.copy_job_for_retry(self.db,jobs.get_job(self.db,out.id))
        self.assertEqual(copied.video_model,"seedance_mini_fal")
        result,shot=jobs.video_source(self.db,out.id,1)
        translated=video.translate(result,shot)
        self.assertEqual(translated["provider"],"fal")
        self.assertEqual(translated["model"],"bytedance/seedance-2.0/mini/reference-to-video")
        self.assertNotIn("audio_urls",translated["request"])
        self.assertTrue(translated["request"]["generate_audio"])
        with patch.object(video.settings,"fal_api_key","test"), patch("app.services.audio_video_service.submit",return_value={"id":"task","status_url":"https://queue.fal.run/status","response_url":"https://queue.fal.run/result"}) as provider:
            video.start(self.db,out.id,1)
            self.assertEqual(provider.call_args.args[0],translated["model"])

    def test_script_submission_keeps_selection(self):
        payload=JobCreate(brief="Lamp",script_text="A lamp glows.",resolutions={"characters":{},"locations":{}},ai_model="Seedance 2.0",video_model="seedance_fast_evolink")
        out=routes.create_job(payload,BackgroundTasks(),self.db)
        self.assertEqual(out.video_model,"seedance_fast_evolink")
        self.assertEqual(jobs.get_job(self.db,out.id).script_text,"A lamp glows.")

    def test_speech_inherits_job_over_previous_shot_choice_and_rejects_override(self):
        shot={**self.shot,"has_dialogue":True,"speech_mode":"onscreen","characters_in_shot":["Meera"],
              "dialogue_audio_url":"https://example.com/audio.wav","dialogue_audio_duration_sec":5,
              "video_audio_model":"seedance_evolink", "dialogue_text":"Hello there."}
        result={"ai_model":"Seedance 2.0","video_model":"seedance_fast_fal","quality":"720p"}
        self.assertEqual(video.translate(result,shot)["model"],"bytedance/seedance-2.0/fast/reference-to-video")
        with self.assertRaisesRegex(ValueError,"inherit"):
            video.translate(result,shot,audio_model="seedance_mini_evolink")

    def test_all_seedance_choices_cover_silent_shots(self):
        from app.services.audio_video_service import MODELS
        for model,(provider,endpoint) in MODELS.items():
            if not model.startswith("seedance_"):continue
            result={"ai_model":"Seedance 2.0","video_model":model,"quality":"720p"}
            out=video.translate(result,self.shot)
            self.assertEqual((out["provider"],out["model"]),(provider,endpoint))
            if provider=="fal":self.assertIn("@Image1",out["request"]["prompt"])

    def test_kling_silent_is_kling_not_seedance(self):
        out=video.translate({"ai_model":"Kling 3.0","video_model":"kling_voice_fal"},self.shot)
        self.assertEqual(out["model"],"fal-ai/kling-video/v3/4k/image-to-video")
        self.assertNotIn("elements",out["request"])
        with self.assertRaisesRegex(ValueError,"only supports"):
            video.translate({"ai_model":"Kling 3.0","video_model":"kling_avatar_fal"},self.shot)

    def test_no_silent_language_or_family_switch(self):
        for kwargs in ({"ai_model":"Kling 3.0","video_model":"kling_voice_fal","language":"Hindi"},
                       {"ai_model":"Seedance 2.0","video_model":"kling_voice_fal"}):
            with self.assertRaises(ValueError):JobCreate(brief="test",**kwargs)

    def test_additive_migration_old_jobs_nullable(self):
        engine=create_engine("sqlite://")
        with engine.begin() as c:c.exec_driver_sql("CREATE TABLE jobs (id VARCHAR PRIMARY KEY, brief TEXT)")
        with patch.object(database,"engine",engine):
            database.ensure_job_intake_columns();database.ensure_job_intake_columns()
        self.assertIn("video_model",[c["name"] for c in inspect(engine).get_columns("jobs")])
        engine.dispose()
