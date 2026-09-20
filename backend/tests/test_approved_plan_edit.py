"""Approved-plan revision: real local API/persistence; no providers called."""
import copy
import json
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.services import job_service as jobs
from app.models import VideoTask
from app.services.video_generation_service import source_fingerprint
import test_module_f


class ApprovedPlanEditTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)
        self.client=TestClient(app)
        with self.sessions() as db:
            job=jobs.get_job(db,self.job_id)
            result=jobs.job_result(job)
            result["assembly"]["provisional"]=False
            for s in result["shots"]:
                s.update(still_frame_url="https://example.com/still.png",compiled_prompt="Old accepted instructions",dialogue_audio_duration_sec=2 if s["has_dialogue"] else 0)
            jobs.set_result(db,self.job_id,result)
            for s in result["shots"]:
                jobs.claim_video(db,self.job_id,s["shot_number"],{"video_status":"done","video_task_id":str(s["shot_number"]),"video_url":"https://example.com/video.mp4","video_source_hash":source_fingerprint(s)})
                jobs.update_video(db,self.job_id,s["shot_number"],video_status="done")
    def tearDown(self):
        self.client.close()
        test_module_f.ModuleFTests.tearDown(self)

    def payload(self, revision=0):
        return {"expected_plan_revision":revision,"shots":[{"shot_number":2,
                "description":"Ravi speaks only the approved line, then rests silently.",
                "dialogue_text":"Enjoy this tea."}]}

    def test_save_reopens_plan_and_preserves_siblings_and_history(self):
        before=self.client.get(f"/api/jobs/{self.job_id}").json()["result"]
        with patch("app.agents.director.call_agent",side_effect=AssertionError("No provider on save")):
            response=self.client.post(f"/api/jobs/{self.job_id}/revise",json=self.payload())
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()["result"]
        self.assertFalse(result["generation_approved"])
        self.assertEqual(result["plan_edited_shots"],[2])
        self.assertEqual(result["plan_revision"],1)
        self.assertEqual(result["shots"][1]["dialogue_text"],"Enjoy this tea.")
        for key in ("video_url","still_frame_url","compiled_prompt","dialogue_audio_url"):
            self.assertNotIn(key,result["shots"][1])
            self.assertEqual(result["shots"][0].get(key), before["shots"][0].get(key))
        self.assertEqual(len(result["plan_revision_history"][0]["videos"]),1)
        with self.sessions() as db:
            self.assertEqual(db.query(VideoTask).filter_by(job_id=self.job_id).count(),2)
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/revise",json=self.payload()).status_code,409)

    def test_active_video_prevents_edit_without_losing_any_output(self):
        with self.sessions() as db:jobs.update_video(db,self.job_id,1,video_status="processing")
        response=self.client.post(f"/api/jobs/{self.job_id}/revise",json=self.payload())
        self.assertEqual(response.status_code,409)
        with self.sessions() as db:
            self.assertEqual(db.query(VideoTask).filter_by(job_id=self.job_id).count(),3)
            self.assertTrue(jobs.job_result(jobs.get_job(db,self.job_id))["generation_approved"])

    def test_approval_preserves_sibling_audio_and_only_queues_preparation(self):
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/revise",json=self.payload()).status_code,200)
        with patch.object(jobs,"queue_pipeline_task") as queue:
            response=self.client.post(f"/api/jobs/{self.job_id}/approve")
        self.assertEqual(response.status_code,200,response.text)
        queue.assert_called_once()
        result=response.json()["result"]
        self.assertEqual(result["plan_edited_shots"],[2])
        contract=result["shots"][1]["still_frame_contract"]
        self.assertEqual(contract["contract_version"],"shot-opening-v2")
        self.assertIn("first physical instant",contract["checks"]["opening_state"])

    def test_noop_preserves_approval_and_media(self):
        original=self.client.get(f"/api/jobs/{self.job_id}").json()["result"]["shots"][1]
        payload={"expected_plan_revision":0,"shots":[{"shot_number":2,"description":original["description"],"dialogue_text":original["dialogue_text"]}]}
        response=self.client.post(f"/api/jobs/{self.job_id}/revise",json=payload)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()["result"]["generation_approved"])
        self.assertEqual(response.json()["result"]["shots"][1]["video_url"],"https://example.com/video.mp4")
