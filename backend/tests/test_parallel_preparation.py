import copy
import threading
import unittest
from unittest.mock import patch, Mock

from app.agents import director


class ParallelPreparationTests(unittest.TestCase):
    def run_case(self, first, failure=None):
        owner = threading.get_ident()
        rendezvous = threading.Barrier(2)
        persisted_first = threading.Event()
        result = {"shots": [{"shot_number": 1, "description": "cup", "duration_sec": 5,
                            "dialogue_audio_url": "original-audio", "compiled_prompt": "stale"}],
                  "assembly": {"transitions": [{"between": "1-2", "type": "cut"}]}}
        states, events = [], []
        def save(db, job_id, current):
            self.assertEqual(threading.get_ident(), owner)
            states.append(copy.deepcopy(current))
            shot = current["shots"][0]
            if (first == "previews" and shot.get("still_frame_url")) or (first == "compiler" and shot.get("compiled_prompt") == "new"):
                persisted_first.set()
        def emit(key, note):
            self.assertEqual(threading.get_ident(), owner)
            events.append((key, note))
        def previews(snapshot, *, on_progress, **kwargs):
            rendezvous.wait(timeout=2)
            if first != "previews":
                self.assertTrue(persisted_first.wait(2))
            if failure == "previews":
                raise RuntimeError("injected preview failure")
            snapshot["shots"][0].update(still_frame_url="accepted-image", still_frame_status="ready",
                compiled_prompt="MUST NOT MERGE", duration_sec=999, dialogue_audio_url="MUST NOT MERGE")
            snapshot["entity_references"] = {"props:cup": {"shot_number": 1, "url": "accepted-image"}}
            on_progress(snapshot)
            return snapshot["shots"]
        def compiler(snapshot, **kwargs):
            rendezvous.wait(timeout=2)
            if first != "compiler":
                self.assertTrue(persisted_first.wait(2))
            if failure == "compiler":
                raise RuntimeError("injected compiler failure")
            snapshot["shots"][0].update(compiled_prompt="new", still_frame_url="MUST NOT MERGE",
                duration_sec=999, dialogue_audio_url="MUST NOT MERGE")
            return snapshot["shots"]
        with patch.object(director.job_service, "set_result", side_effect=save), \
             patch.object(director.job_service, "set_status") as status, \
             patch.object(director, "generate_still_frames", side_effect=previews), \
             patch.object(director, "compile_shot_prompts", side_effect=compiler):
            director._prepare_media_parallel(Mock(), "audit", result, brief="test", emit=emit)
        shot = result["shots"][0]
        self.assertEqual(shot["duration_sec"], 5)
        self.assertEqual(shot["dialogue_audio_url"], "original-audio")
        self.assertEqual(result["assembly"]["transitions"][0]["type"], "cut")
        self.assertFalse(result["video_prompts_pending"])
        self.assertTrue(persisted_first.is_set())
        if failure != "previews":
            self.assertEqual(shot["still_frame_url"], "accepted-image")
            self.assertIn("props:cup", result["entity_references"])
        else:
            self.assertEqual(shot["still_frame_status"], "failed")
        if failure != "compiler":
            self.assertEqual(shot["compiled_prompt"], "new")
        else:
            self.assertNotIn("compiled_prompt", shot)
            self.assertIn("video_prompt_error", result)
            status.assert_called_once()
        self.assertEqual(sum(k == "media_preparation_timing" and '"finished"' in n for k,n in events), 2)

    def test_preview_finishes_first_and_is_visible_while_compiler_runs(self):
        self.run_case("previews")

    def test_compiler_finishes_first_and_late_preview_does_not_overwrite_it(self):
        self.run_case("compiler")

    def test_compiler_failure_preserves_accepted_preview(self):
        self.run_case("previews", "compiler")

    def test_preview_failure_preserves_compiled_prompt(self):
        self.run_case("compiler", "previews")

    def test_revision_compiles_and_prepares_only_changed_shot(self):
        result = {"plan_edited_shots": [2], "shots": [
            {"shot_number": 1, "review_mode": "user", "compiled_prompt": "keep", "still_frame_url": "keep-image", "dialogue_audio_url": "keep-audio"},
            {"shot_number": 2, "review_mode": "user", "description": "changed"}]}
        def previews(snapshot, *, shot_numbers, **kwargs):
            self.assertEqual(shot_numbers, {2})
            snapshot["shots"][1]["still_frame_url"] = "new-image"
            return snapshot["shots"]
        def compiler(snapshot, **kwargs):
            self.assertEqual([s["shot_number"] for s in snapshot["shots"]], [2])
            return [{**snapshot["shots"][0], "compiled_prompt": "new"}]
        with patch.object(director.job_service, "set_result"), \
             patch.object(director, "generate_still_frames", side_effect=previews), \
             patch.object(director, "compile_shot_prompts", side_effect=compiler):
            director._prepare_media_parallel(Mock(), "audit", result, brief="test", emit=Mock())
        self.assertEqual(result["shots"][0]["compiled_prompt"], "keep")
        self.assertEqual(result["shots"][0]["still_frame_url"], "keep-image")
        self.assertEqual(result["shots"][0]["dialogue_audio_url"], "keep-audio")
        self.assertEqual(result["shots"][1]["compiled_prompt"], "new")
        self.assertNotIn("plan_edited_shots", result)

    def test_second_phase_never_regenerates_an_accepted_early_preview(self):
        result = {"shots": [
            {"shot_number": 1, "description": "accepted", "still_frame_url": "keep-image",
             "still_frame_key": "keep-key", "compiled_prompt": "old"},
            {"shot_number": 2, "description": "missing", "compiled_prompt": "old"}]}
        def previews(snapshot, *, shot_numbers, **kwargs):
            self.assertEqual(shot_numbers, {2})
            snapshot["shots"][1].update(still_frame_url="new-image", still_frame_status="ready")
            return snapshot["shots"]
        def compiler(snapshot, **kwargs):
            return [{**shot, "compiled_prompt": "new"} for shot in snapshot["shots"]]
        with patch.object(director.job_service, "set_result"), \
             patch.object(director, "generate_still_frames", side_effect=previews), \
             patch.object(director, "compile_shot_prompts", side_effect=compiler):
            director._prepare_media_parallel(Mock(), "audit", result, brief="test", emit=Mock())
        self.assertEqual(result["shots"][0]["still_frame_url"], "keep-image")
        self.assertEqual(result["shots"][1]["still_frame_url"], "new-image")
