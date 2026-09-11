"""Integrated endpoint tests. Provider responses are fixtures, never live evidence."""
import copy
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
        self.url = f"/api/jobs/{self.job_id}/shots/2/regenerate"

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.session_patch.stop()
        self.engine.dispose()

    def audio_patches(self):
        return (
            patch.object(voice_generation_service, "synthesize_dialogue", return_value=voice_generation_service.GeneratedAudio(b"fixture", "audio/wav", ".wav", "fixture")),
            patch("app.services.storage_service.upload_bytes", return_value={"url": "https://example.invalid/new.wav"}),
        )

    def test_no_hint_preserves_shots_and_runs_existing_audio(self):
        audio_patch, storage_patch = self.audio_patches()
        with patch("app.routes.jobs.call_agent") as cine, patch("app.agents.director.call_agent") as qa, audio_patch as audio, storage_patch:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        cine.assert_not_called()
        qa.assert_not_called()
        audio.assert_awaited_once()
        final = self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        for index in (0, 2):
            self.assertEqual(final["shots"][index], self.original["shots"][index])
        self.assertEqual(final["shots"][1]["status"], "done")
        self.assertEqual(final["shots"][1]["dialogue_audio_url"], "https://example.invalid/new.wav")

    def test_hint_qa_autocorrection_then_real_voice_service(self):
        # Initial fixture crosses the axis; the EXISTING director QA loop repairs it.
        unsafe = copy.deepcopy(self.original["shots"])
        unsafe[1]["camera_movement"] = "360-degree orbit across dialogue axis"
        unsafe[1]["dialogue_text"] = "Unauthorized rewrite"
        unsafe[0]["lighting"] = "Unauthorized neighbor rewrite"
        fixed = copy.deepcopy(self.original["shots"])
        fixed[1]["camera_movement"] = "short arc south of axis"
        responses = [
            {"approved": False, "issues": [{"shot_number": 2, "problem": "Orbit crosses dialogue axis", "fix_instruction": "Limit arc to south side"}]},
            {"shots": fixed}, {"approved": True, "issues": []},
            self.original["assembly"],
        ]
        audio_patch, storage_patch = self.audio_patches()
        with patch("app.routes.jobs.call_agent", return_value={"shots": unsafe}) as cine, patch("app.agents.director.call_agent", side_effect=responses) as qa, audio_patch as audio, storage_patch:
            response = self.client.post(self.url, json={"movement": "orbit"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(cine.call_args.args[0], prompts.CINEMATOGRAPHY_FIX)
        self.assertIn('Optional style hints: {"movement": "orbit"}', cine.call_args.args[1])
        self.assertIs(qa.call_args_list[1].args[0], prompts.CINEMATOGRAPHY_FIX)
        self.assertIn("Required fixes:", qa.call_args_list[1].args[1])
        final = self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        self.assertEqual(final["shots"][1]["camera_movement"], "short arc south of axis")
        self.assertEqual(final["shots"][1]["dialogue_text"], "Try this tea.")
        self.assertEqual(final["shots"][0], self.original["shots"][0])
        self.assertEqual(final["shots"][2], self.original["shots"][2])
        audio.assert_awaited_once()
        self.assertEqual(audio.call_args.kwargs["text"], "Try this tea.")
        self.assertEqual(final["shots"][1]["status"], "done")

    def test_invalid_hint_and_locked_revise_fields_return_422(self):
        for payload in ({"movement": "anything"}, {"camera_angle": "lowangle"}):
            self.assertEqual(self.client.post(self.url, json=payload).status_code, 422)
        for field in ("camera_angle", "camera_movement", "lighting", "composition_note"):
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
