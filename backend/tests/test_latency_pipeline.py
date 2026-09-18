import copy
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job
from app.agents.execution import execute, planning_execution
from app.agents import llm_client, prompts
from app.services import job_service, still_frame_service as still
from app.services.character_image_service import GeneratedCharacterImage


class LatencyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(Job(id="test", brief="test"))
        self.db.commit()
        raw = io.BytesIO()
        Image.new("RGB", (160, 90)).save(raw, format="PNG")
        self.image = GeneratedCharacterImage(raw.getvalue(), "image/png")
        self.result = {"aspect_ratio": "16:9", "continuity": {"characters": [], "props": [], "visual_style": {"rendering": "natural"}},
            "shots": [{"shot_number": n, "scene_number": n, "description": f"Object {n} on a table",
                       "characters_in_shot": [], "camera_angle": "close-up", "camera_movement": "static"} for n in (1, 2, 3)]}

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_checkpoint_reuses_only_exact_input_and_retry_copy(self):
        calls, events = [], []
        def call(system, content, **kw):
            calls.append(content)
            kw["on_response"]({"stop_reason": "end_turn"})
            return {"approved": True, "ok": content}
        owner = threading.get_ident()
        def emit(k, n):
            self.assertEqual(threading.get_ident(), owner)
            events.append(json.loads(n))
        scope = (self.db, "test", emit, owner)
        for content in ("a", "a", "b"):
            execute(call, prompts.QA_AGENT, content, {}, scope)
        self.assertEqual(calls, ["a", "b"])
        self.assertTrue(any(e.get("phase") == "checkpoint_reused" for e in events))
        retried = job_service.copy_job_for_retry(self.db, self.db.get(Job, "test"))
        execute(call, prompts.QA_AGENT, "a", {}, (self.db, retried.id, emit, owner))
        self.assertEqual(calls, ["a", "b"])

    def test_invalid_coverage_is_not_cached_across_retry(self):
        content = json.dumps({'ad_direction':{'takeaway':'Test'},
            'shots':[{'shot_number':1,'scene_number':1}],
            'approved_story':{'scenes':[{'scene_number':1,'description':'An action'}]}})
        calls = []
        def provider(*args, **kwargs):
            calls.append(1)
            result = {'approved':True,'issues':[]}
            if len(calls) > 1:
                result['scene_coverage'] = [{'scene_number':1,'shot_numbers':[1],
                    'covered':True,'evidence':'Action shown'}]
            return result
        scope = (self.db, 'test', lambda *args: None, threading.get_ident())
        with self.assertRaisesRegex(ValueError, 'omitted scene coverage'):
            execute(provider, prompts.QA_AGENT, content, {}, scope)
        self.assertTrue(execute(provider, prompts.QA_AGENT, content, {}, scope)['approved'])
        execute(provider, prompts.QA_AGENT, content, {}, scope)
        self.assertEqual(len(calls), 2)

    def test_independent_previews_overlap_without_compilation_and_persist_on_owner(self):
        barrier = threading.Barrier(2)
        active, peak = 0, 0
        lock = threading.Lock()
        owner = threading.get_ident()
        calls = []
        def generate(visual, *a, **kw):
            nonlocal active, peak
            self.assertIn("approved shot", visual)
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(visual)
                i = len(calls)
            if i <= 2:
                barrier.wait(timeout=2)
            time.sleep(.02)
            with lock:
                active -= 1
            return self.image
        def persist(result):
            self.assertEqual(threading.get_ident(), owner)
        with patch.object(still, "generate_still", side_effect=generate), patch.object(still, "check_still", return_value={"approved": True, "reason": "ok"}), patch.object(still.storage_service, "upload_bytes", side_effect=lambda **k: {"url": "https://image/" + k["key"], "key": k["key"]}):
            still.generate_still_frames(self.result, job_id="test", emit=lambda *a: None, on_progress=persist)
            self.assertEqual(peak, 2)
            self.assertTrue(all(s.get("still_frame_url") and not s.get("compiled_prompt") for s in self.result["shots"]))
            urls = [s["still_frame_url"] for s in self.result["shots"]]
            still.generate_still_frames(self.result, job_id="test", emit=lambda *a: None)
            self.assertEqual(len(calls), 3)
            self.assertEqual(urls, [s["still_frame_url"] for s in self.result["shots"]])

    def test_shared_entity_waits_for_accepted_anchor_and_failed_retry_preserves_sibling(self):
        self.result["continuity"]["props"] = [{"name": "Cup"}]
        self.result["shots"] = self.result["shots"][:2]
        for s in self.result["shots"]:
            s["description"] = "Cup on table"
        seen = []
        def generate(visual, refs, *a, **kw):
            seen.append(refs)
            if len(seen) == 2:
                self.assertTrue(any("props:cup" in ref[0] for ref in refs))
            return self.image
        with patch.object(still, "generate_still", side_effect=generate), patch.object(still, "_download_reference_image", return_value=self.image), patch.object(still, "check_still", return_value={"approved": True, "reason": "ok", "visible_entities": ["props:cup"]}), patch.object(still.storage_service, "upload_bytes", side_effect=lambda **k: {"url": "https://image/"+k["key"], "key": k["key"]}):
            still.generate_still_frames(self.result, job_id="test", emit=lambda *a: None)
        sibling = copy.deepcopy(self.result["shots"][0])
        self.result["shots"][1].update(still_frame_url=None, still_frame_status="failed")
        with patch.object(still, "generate_still", side_effect=still.StillFrameError("forced")), patch.object(still, "_download_reference_image", return_value=self.image):
            still.generate_still_frames(self.result, job_id="test", emit=lambda *a: None, shot_numbers={2})
        self.assertEqual(self.result["shots"][0], sibling)
        self.assertEqual(self.result["shots"][1]["still_frame_status"], "failed")
        self.assertNotIn("still_frame_key", self.result["shots"][1])

    def test_style_change_invalidates_cached_image(self):
        from app.services.preview_plan import preview_input
        shot = self.result["shots"][0]
        shot["preview_input"] = preview_input(self.result, shot)
        shot.update(still_frame_url="old", still_frame_key="old", still_frame_source_hash=still.shot_fingerprint(shot))
        self.result["continuity"]["visual_style"]["rendering"] = "anime"
        still.invalidate_changed_stills(self.result)
        self.assertIsNone(shot["still_frame_url"])

    def test_durable_dispatch_single_claim(self):
        job_service.queue_pipeline_task(self.db, "test", "plan")
        with self.assertRaisesRegex(ValueError, "already running"):
            job_service.queue_pipeline_task(self.db, "test", "plan")
        claimed = job_service.claim_pipeline_task(self.db)
        self.assertEqual(claimed[0], "test")
        self.assertIsNone(job_service.claim_pipeline_task(self.db))

    def test_request_can_complete_after_old_two_thirds_cutoff(self):
        events, budgets = [], []
        def provider(*args, **kwargs):
            budgets.append(kwargs["request_timeout"])
            time.sleep(.15)
            return {"approved": True}
        scope = (self.db, "test", lambda k,n: events.append(json.loads(n)), threading.get_ident())
        with patch("app.agents.execution.stage_budget", return_value=.2):
            result = execute(provider, prompts.QA_AGENT, "full-budget", {}, scope)
        self.assertTrue(result["approved"])
        self.assertGreater(budgets[0], .19)
        self.assertEqual(len(budgets), 1)
        self.assertEqual(events[-1]["phase"], "completed")

    def test_early_transient_failure_retries_with_remaining_budget(self):
        import anthropic, httpx
        budgets = []
        def provider(*args, **kwargs):
            budgets.append(kwargs["request_timeout"])
            if len(budgets) == 1:
                raise anthropic.RateLimitError("busy", response=httpx.Response(429,
                    request=httpx.Request("POST", "https://example.test")), body=None)
            return {"approved": True}
        scope = (self.db, "test", lambda *args: None, threading.get_ident())
        with patch("app.agents.execution.stage_budget", return_value=2):
            self.assertTrue(execute(provider, prompts.QA_AGENT, "rate-limit", {}, scope)["approved"])
        self.assertEqual(len(budgets), 2)
        self.assertLess(budgets[1], budgets[0])

    def test_hard_timeout_never_saves_late_response_or_launches_duplicate(self):
        events, calls = [], []
        def provider(*args, **kwargs):
            calls.append(1)
            time.sleep(.08)
            return {"approved": True}
        scope = (self.db, "test", lambda k,n: events.append(json.loads(n)), threading.get_ident())
        with patch("app.agents.execution.stage_budget", return_value=.03):
            with self.assertRaisesRegex(TimeoutError, "completed steps are saved"):
                execute(provider, prompts.QA_AGENT, "test", {}, scope)
        time.sleep(.1)
        self.assertEqual(calls, [1])
        from app.models import AgentCheckpoint
        self.assertEqual(self.db.query(AgentCheckpoint).count(), 0)
        self.assertEqual(events[-1]["phase"], "timeout")

    def test_worker_failure_releases_busy_flags_and_preserves_preview(self):
        job = self.db.get(Job, "test")
        job.result_json = json.dumps({"preview_preparation_pending": True, "video_prompts_pending": True,
            "shots": [{"shot_number": 1, "still_frame_url": "accepted"},
                      {"shot_number": 2, "still_frame_status": "pending"}]})
        self.db.commit()
        job_service.fail_pipeline_preparation(self.db, "test", "Preparation failed; retry.")
        result = json.loads(job.result_json)
        self.assertFalse(result["preview_preparation_pending"])
        self.assertFalse(result["video_prompts_pending"])
        self.assertEqual(result["shots"][0]["still_frame_url"], "accepted")
        self.assertEqual(result["shots"][1]["still_frame_status"], "failed")
        self.assertEqual(job.status, "error")

    def test_approval_does_not_race_completed_plan_dispatch_cleanup(self):
        from app.models import PipelineTask
        job_service.queue_pipeline_task(self.db, "test", "plan")
        _, old_token, _ = job_service.claim_pipeline_task(self.db)
        job_service.set_status(self.db, "test", "done")
        job_service.queue_pipeline_task(self.db, "test", "prepare")
        job_service.heartbeat_pipeline_task(self.db, "test", old_token, "complete")
        self.db.expire_all()
        self.assertEqual(self.db.get(PipelineTask, "test").status, "queued")
        self.assertEqual(job_service.claim_pipeline_task(self.db)[2], "prepare")

    def test_expired_lease_preserves_output_without_resubmitting(self):
        from datetime import datetime, timedelta
        from app.models import PipelineTask
        job_service.queue_pipeline_task(self.db, "test", "plan")
        job_service.claim_pipeline_task(self.db)
        job = self.db.get(Job, "test")
        job.result_json = json.dumps({"shots": [{"shot_number": 1, "still_frame_url": "accepted"},
            {"shot_number": 2, "still_frame_status": "generating"}], "audio_assembly_pending": True})
        row = self.db.get(PipelineTask, "test")
        row.heartbeat_at = datetime.utcnow() - timedelta(seconds=100)
        self.db.commit()
        job_service.pause_expired_pipeline_tasks(self.db)
        self.db.expire_all()
        self.assertEqual(self.db.get(PipelineTask, "test").status, "paused")
        job = self.db.get(Job, "test")
        result = json.loads(job.result_json)
        self.assertEqual(job.status, "error")
        self.assertEqual(result["shots"][0]["still_frame_url"], "accepted")
        self.assertEqual(result["shots"][1]["still_frame_status"], "failed")
        self.assertFalse(result["audio_assembly_pending"])
        self.assertIsNone(job_service.claim_pipeline_task(self.db))
