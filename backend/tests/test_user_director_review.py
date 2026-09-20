"""User approval replaces text QA; no paid provider calls in these tests."""
import copy
import unittest
from unittest.mock import patch
from fastapi import BackgroundTasks, HTTPException
from app.agents import director
from app.services import director_review, job_service
from app.services.shot_prompt_compiler import compile_shot_prompts
from app.routes.jobs import revise_job, approve_job
from app.schemas import JobRevise
from test_ad_direction import directed
import test_module_f

class UserReviewTests(unittest.TestCase):
    def test_valid_plan_never_calls_semantic_qa_or_assembler(self):
        result = directed()
        with patch.object(director, "call_agent", side_effect=AssertionError("No model review")):
            checked = director.validate_and_correct(result["shots"], [], 5)
            self.assertTrue(checked["qa"]["approved"])
            self.assertFalse(checked["qa"]["semantic_review_performed"])
            assembled = director.assemble_shots(checked["shots"], [], 1)
            self.assertEqual(assembled["shots"][0]["duration_sec"], 5)

    def test_missing_fields_return_editable_issues_without_rewriting(self):
        result = directed(); result["shots"][0]["lighting"] = ""
        original = copy.deepcopy(result["shots"])
        checked = director.validate_and_correct(result["shots"], [], 5)
        self.assertFalse(checked["qa"]["approved"])
        self.assertEqual(checked["shots"][0]["description"], original[0]["description"])
        self.assertEqual(checked["shots"][0]["lighting"], "")

    def test_edited_shot_review_ignores_unchanged_sibling_findings(self):
        result = directed()
        sibling = {**copy.deepcopy(result['shots'][0]), 'shot_number': 2, 'lighting': ''}
        checked = director.validate_and_correct(
            [result['shots'][0], sibling], [], 10,
            semantic_review=False, review_shot_numbers={1})
        self.assertTrue(checked['qa']['approved'])
        self.assertEqual(checked['shots'][1]['lighting'], '')

    def test_multi_character_speaker_must_be_explicit_and_voice_matches(self):
        shot = directed()["shots"][0]
        shot.update(characters_in_shot=["A", "B"], speech_mode="onscreen", opening_characters=["A"], direction_source=None)
        cast = [{"name":"A","voice_sample_ref":"a"},{"name":"B","voice_sample_ref":"b"}]
        self.assertFalse(director_review.review([shot], cast)["approved"])
        shot["speaker_name"] = "B"
        self.assertTrue(director_review.review([shot], cast)["approved"])
        self.assertEqual(director._attach_voice_refs([shot], cast)[0]["voice_refs"], {"B":"b"})

    def test_semantic_mechanics_reject_ambiguous_onscreen_speaker(self):
        from app.services.planning_contract import check_mechanics
        shot = directed()["shots"][0]
        shot.update(has_dialogue=True, speech_mode="onscreen", speaker_name="",
                    characters_in_shot=["A", "B"], opening_characters=["A", "B"])
        verdict = check_mechanics({"approved": True, "issues": []}, [shot],
            [{"name": "A"}, {"name": "B"}], 5)
        self.assertFalse(verdict["approved"])
        self.assertIn("exact speaker_name", verdict["issues"][0]["problem"])

    def test_approved_plan_serializes_without_model_call_and_preserves_speech(self):
        result = directed()
        result.update(director.validate_and_correct(result["shots"], [], 5))
        result["assembly"]["provisional"] = False
        result["shots"].append({**copy.deepcopy(result["shots"][0]), "shot_number":2})
        result["assembly"] = director_review.timeline(result["shots"])
        out = compile_shot_prompts(result, emit=lambda *a:None,
            call_agent=lambda *a,**k: (_ for _ in ()).throw(AssertionError("No model call")))
        self.assertEqual(len(out),2)
        for shot in out:
            self.assertIn("Look here.",shot["compiled_prompt"])
            self.assertIn("An unhurried hand lifts the cup",shot["compiled_prompt"])
            self.assertIn("Render style:",shot["compiled_prompt"])
            self.assertIn("Camera direction:",shot["compiled_prompt"])

    def test_structured_beats_override_stale_card_action_synopsis(self):
        result = directed()
        shot = result["shots"][0]
        shot["description"] = "Obsolete instruction: speak before the handoff."
        shot.pop("direction_source", None)
        shot["shot_direction"].update(
            blocking="Woman beside the man.",
            action_beats=["Hand over the bottle silently.", "Speak the approved line only after drinking."],
            critical_outcome="The man receives the bottle.")
        result.update(director.validate_and_correct(result["shots"], [], 5))
        result["assembly"]["provisional"] = False
        out = compile_shot_prompts(result, emit=lambda *a:None,
            call_agent=lambda *a, **k: (_ for _ in ()).throw(AssertionError("No model call")))
        prompt = out[0]["compiled_prompt"]
        self.assertNotIn("Obsolete instruction", prompt)
        self.assertIn("Hand over the bottle silently", prompt)
        self.assertIn("Speak the approved line only after drinking", prompt)
        self.assertEqual(out[0]["description"], shot["description"])

    def test_semantically_reviewed_direction_serializes_boundaries_without_second_model(self):
        result = directed()
        result["shots"][0].pop("review_mode", None)
        second = {**copy.deepcopy(result["shots"][0]), "shot_number": 2,
            "scene_number": 2,
            "state_at_shot_start": "Blue cup held above table.",
            "state_at_shot_end": "Blue cup rests on saucer."}
        second.pop("review_mode", None)
        result["shots"].append(second)
        result["assembly"] = {"provisional": False, "total_duration_sec": 10, "transitions": [{
            "between": "1-2", "type": "cut", "reason": "Continue the same cup movement"}]}
        out = compile_shot_prompts(result, emit=lambda *a: None,
            call_agent=lambda *a, **k: (_ for _ in ()).throw(AssertionError("No compiler model")))
        self.assertIn("Incoming continuity:", out[1]["compiled_prompt"])
        self.assertIn("Continue the same cup movement", out[1]["compiled_prompt"])
        self.assertIn("Outgoing transition:", out[0]["compiled_prompt"])

class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)
        self.plan = directed(); self.plan["generation_approved"] = False
        self.plan["format"]["duration_target_sec"] = 5
        self.plan.update(director.validate_and_correct(self.plan["shots"], [], 5))
        with self.sessions() as db: job_service.set_result(db,self.job_id,self.plan)
    def tearDown(self): test_module_f.ModuleFTests.tearDown(self)

    def test_execution_edit_persists_in_plan_without_provider_call(self):
        direction={**self.plan['shots'][0]['shot_direction'], 'blocking':'Hand enters screen right.',
            'action_beats':['Hand grips handle.','Cup lifts and holds.'], 'critical_outcome':'Cup clears table.'}
        with patch.object(director,'call_agent',side_effect=AssertionError('No model call')):
            with self.sessions() as db:
                revised=revise_job(self.job_id,JobRevise(shots=[dict(shot_number=1,shot_direction=direction,
                    description=self.plan['shots'][0]['description'],dialogue_text=self.plan['shots'][0]['dialogue_text'])]),db)
                self.assertEqual(revised.result['shots'][0]['shot_direction'],direction)
                saved=job_service.get_job(db,self.job_id)
                import json
                self.assertEqual(json.loads(saved.result_json)['shots'][0]['shot_direction'],direction)
                self.assertFalse(revised.result['generation_approved'])

    def test_edit_persists_without_generation_then_approval_queues_once(self):
        with patch.object(director,"call_agent",side_effect=AssertionError("No model call")):
            with self.sessions() as db:
                revised = revise_job(self.job_id,JobRevise(shots=[dict(shot_number=1,
                    description="Hand sets down the cup", dialogue_text="Keep my exact words.",
                    lighting="Soft evening light",duration_sec=6)]),db)
                self.assertEqual(revised.result["shots"][0]["lighting"],"Soft evening light")
                self.assertFalse(revised.result["generation_approved"])
                with patch.object(job_service,"queue_pipeline_task") as queue:
                    approve_job(self.job_id,BackgroundTasks(),db)
                    approve_job(self.job_id,BackgroundTasks(),db)
                    queue.assert_called_once()
                with self.assertRaises(HTTPException) as error:
                    revise_job(self.job_id,JobRevise(shots=[]),db)
                self.assertEqual(error.exception.status_code,409)

    def test_invalid_edit_is_saved_but_approval_cannot_generate(self):
        with self.sessions() as db:
            revised = revise_job(self.job_id,JobRevise(shots=[dict(shot_number=1,
                description="Hand lifts cup",dialogue_text="Look here.",duration_sec=-1)]),db)
            self.assertFalse(revised.result["qa"]["approved"])
            with patch.object(job_service,"queue_pipeline_task") as queue:
                with self.assertRaises(HTTPException) as error: approve_job(self.job_id,BackgroundTasks(),db)
                self.assertEqual(error.exception.status_code,422)
                queue.assert_not_called()

if __name__ == "__main__": unittest.main()
