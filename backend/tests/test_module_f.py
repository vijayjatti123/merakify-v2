"""Integrated endpoint tests. Provider responses are fixtures, never live evidence."""
import copy
from test_voice_generation import pcm
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db import Base, get_db
from app.agents import prompts
from app.services import job_service, voice_generation_service


def shot_fixture():
    shots = []
    for number, angle in [(1, "wide eye level"), (2, "medium over shoulder"), (3, "close-up eye level")]:
        shots.append(dict(
            shot_number=number, scene_number=1, camera_angle=angle,
            camera_movement="static", lens="50mm", lighting="soft daylight",
            composition_note="Ravi left, Maya right; south axis",
            duration_sec=5, description="Ravi left and Maya right exchange a cup.",
            characters_in_shot=["Ravi", "Maya"], has_dialogue=number == 2,
            dialogue_text="Try this tea." if number == 2 else "",
            speaker_name="Ravi" if number == 2 else "",
            voice_refs={"Ravi": "rahul"} if number == 2 else {},
            status="done", dialogue_audio_url="https://example.invalid/old.wav" if number == 2 else None,
            dialogue_audio_provider="fixture" if number == 2 else None,
        ))
    return dict(
        generation_approved=True, shots=shots,
        script={"logline": "Ravi offers Maya tea without crossing the dialogue axis."},
        continuity={"characters": [{"name": "Ravi", "voice_sample_ref": "rahul"}, {"name": "Maya", "voice_sample_ref": "priya"}]},
        format={"duration_target_sec": 15}, qa={"approved": True, "issues": []},
        assembly={"total_duration_sec": 15, "transitions": []},
    )


class ModuleFTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        def dependency():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = dependency
        self.session_patch = patch("app.routes.jobs.SessionLocal", self.sessions)
        self.session_patch.start()
        with self.sessions() as db:
            job = job_service.create_job(db, "Module F synthetic continuity fixture")
            self.job_id = job.id
            self.original = shot_fixture()
            job_service.set_result(db, job.id, self.original)
            job_service.set_status(db, job.id, "done")
        self.client = TestClient(app)
        self.original = self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        # These endpoint tests exercise audio recovery, not image/video providers.
        self.media_patch = patch("app.agents.director._prepare_media_parallel")
        self.media_patch.start()
        self.url = f"/api/jobs/{self.job_id}/shots/2/regenerate"

    def tearDown(self):
        self.media_patch.stop()
        self.client.close()
        app.dependency_overrides.clear()
        self.session_patch.stop()
        self.engine.dispose()

    def audio_patches(self):
        return (
            patch.object(voice_generation_service, "synthesize_dialogue", return_value=voice_generation_service.GeneratedAudio(pcm(), "audio/wav", ".wav", "fixture")),
            patch("app.services.storage_service.upload_bytes", return_value={"url": "https://example.invalid/new.wav"}),
        )

    def test_no_hint_preserves_shots_and_runs_existing_audio(self):
        audio_patch, storage_patch = self.audio_patches()
        with patch("app.routes.jobs.call_agent") as cine, patch("app.agents.director.call_agent", return_value=self.original["assembly"]) as qa, audio_patch as audio, storage_patch:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        cine.assert_not_called()
        self.assertEqual([c.args[0] for c in qa.call_args_list], [prompts.SHOT_ASSEMBLER])
        audio.assert_awaited_once()
        final = self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        for index in (0, 2):
            self.assertEqual(final["shots"][index], self.original["shots"][index])
        self.assertEqual(final["shots"][1]["status"], "done")
        self.assertEqual(final["shots"][1]["dialogue_audio_url"], "https://example.invalid/new.wav")

    def test_hint_preserves_speech_and_siblings_without_semantic_rewrite(self):
        candidate = copy.deepcopy(self.original["shots"])
        candidate[1]["camera_movement"] = "slow push in"
        candidate[1]["dialogue_text"] = "Unauthorized rewrite"
        candidate[0]["lighting"] = "Unauthorized neighbor rewrite"
        audio_patch, storage_patch = self.audio_patches()
        with patch("app.routes.jobs.call_agent", return_value={"shots":candidate}) as cine, patch("app.agents.director.call_agent", return_value=self.original["assembly"]) as calls, audio_patch as audio, storage_patch:
            response = self.client.post(self.url, json={"movement":"dollyin"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIs(cine.call_args.args[0],prompts.CINEMATOGRAPHY_FIX)
        self.assertNotIn(prompts.QA_AGENT,[c.args[0] for c in calls.call_args_list])
        final=self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        self.assertEqual(final["shots"][1]["dialogue_text"],"Try this tea.")
        for index in (0,2):self.assertEqual(final["shots"][index],self.original["shots"][index])
        audio.assert_awaited_once()
        self.assertEqual(audio.call_args.kwargs["text"],"Try this tea.")

    def test_invalid_hint_and_locked_revise_fields_return_422(self):
        for payload in ({"movement": "anything"}, {"camera_angle": "lowangle"}):
            self.assertEqual(self.client.post(self.url, json=payload).status_code, 422)
        for field in ("camera_movement", "video_url", "compiled_prompt"):
            response = self.client.post(f"/api/jobs/{self.job_id}/revise", json={"shots": [{"shot_number": 2, "description": "Test", "dialogue_text": "Test", field: "forbidden"}]})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"][0]["type"], "extra_forbidden")

    def test_failed_hint_never_persists_or_queues_audio(self):
        with patch("app.routes.jobs.call_agent", side_effect=ValueError("provider failure")), patch("app.routes.jobs._run_voice_generation_in_background") as audio:
            response = self.client.post(self.url, json={"angle": "lowangle"})
        self.assertEqual(response.status_code, 502)
        audio.assert_not_called()
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}").json()["result"], self.original)

    def test_unapproved_or_out_of_scope_qa_correction_is_not_saved(self):
        for approved in (False, True):
            checked = copy.deepcopy(self.original)
            if approved:
                checked["shots"][0]["lighting"] = "Unrequested neighbor change"
            checked["qa"]["approved"] = approved
            with patch("app.routes.jobs.call_agent", return_value={"shots": self.original["shots"]}), patch("app.routes.jobs.validate_and_correct", return_value=checked), patch("app.routes.jobs._run_voice_generation_in_background") as audio:
                response = self.client.post(self.url, json={"angle": "lowangle"})
            self.assertEqual(response.status_code, 502)
            audio.assert_not_called()
            self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}").json()["result"], self.original)


if __name__ == "__main__":
    unittest.main()
