import json
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import still_frame_service as service
from app.services import job_service
from app.services.character_image_service import GeneratedCharacterImage


class StillFramesTests(unittest.TestCase):
    def setUp(self):
        self.result = {"aspect_ratio": "9:16", "continuity": {"characters": []}, "shots": [
            {"shot_number": 1, "compiled_prompt": "A blue cup on a table. No on-screen text, logos or readable signage; composite text in post.",
             "characters_in_shot": []}]}
        buffer = io.BytesIO()
        service.Image.new("RGB", (90, 160)).save(buffer, format="PNG")
        self.image = GeneratedCharacterImage(buffer.getvalue(), "image/png")
        self.emit = Mock()

    def run_stills(self):
        return service.generate_still_frames(self.result, job_id="audit", emit=self.emit)

    def continuous_shots(self, scenes):
        self.result['shots'] = [{**self.result['shots'][0], 'shot_number': n, 'scene_number': scene}
                                for n, scene in enumerate(scenes, 1)]
        self.result['assembly'] = {'transitions': [
            {'between': f'{n}-{n+1}', 'type': 'cut', 'reason': 'continuous action'}
            for n in range(1, len(scenes))]}

    @patch.object(service, 'check_still', return_value={'approved': True, 'reason': 'Matches'})
    def test_previous_uploaded_image_is_additive_and_sequential(self, check):
        self.continuous_shots([1, 1, 2, 2])
        self.result['continuity']['characters'] = [{'name': 'Meera', 'character_id': 'vault', 'image_url': 'https://reference'}]
        self.result['continuity']['props'] = [{'name': 'Cup'}]
        for shot in self.result['shots']:
            shot['characters_in_shot'] = ['Meera']
        check.return_value['visible_entities'] = ['props:cup']
        order = []
        def generate(visual, refs, aspect, feedback, *, continuation):
            number = 1 + sum(item.startswith('generate') for item in order)
            order.append(f'generate{number}')
            self.assertEqual(refs[0], ('Meera', self.image))
            if number > 1:
                self.assertTrue(any('Job entity props:cup' in name for name, _ in refs))
            if number in (2, 4):
                self.assertEqual(continuation, (number - 1, self.image))
                self.assertIn(f'upload{number-1}', order)
            else:
                self.assertIsNone(continuation)
            return self.image
        def upload(**kwargs):
            number = len([x for x in order if x.startswith('upload')]) + 1
            order.append(f'upload{number}')
            return {'url': f'https://stored/{number}', 'key': str(number)}
        with patch.object(service, '_download_reference_image', return_value=self.image), \
             patch.object(service, 'generate_still', side_effect=generate), \
             patch.object(service.storage_service, 'upload_bytes', side_effect=upload):
            self.run_stills()
        self.assertEqual(order, ['generate1','upload1','generate2','upload2','generate3','upload3','generate4','upload4'])
        self.assertEqual([c.kwargs['continuation'] for c in check.call_args_list],
                         [None, (1,self.image), None, (3,self.image)])

    @patch.object(service.storage_service, 'upload_bytes', return_value={'url': 'https://stored', 'key': 'stored'})
    @patch.object(service, 'check_still', return_value={'approved': True, 'reason': 'Matches'})
    def test_failed_previous_shot_breaks_chain_without_blocking_next(self, check, upload):
        self.continuous_shots([1,1,1])
        calls = []
        def generate(*args, continuation=None):
            calls.append(continuation)
            if len(calls) == 2:
                raise service.StillFrameError('forced failure')
            return self.image
        with patch.object(service, 'generate_still', side_effect=generate):
            shots=self.run_stills()
        self.assertEqual(calls, [None,(1,self.image),None])
        self.assertIsNone(shots[1]['still_frame_url'])
        self.assertEqual(shots[2]['still_frame_url'], 'https://stored')
        self.assertIn('continuing without an action anchor', str(self.emit.call_args_list))

    def test_continuity_gate_rejects_unknown_scene_dissolve_and_time_jump(self):
        self.continuous_shots([1,1])
        a,b=self.result['shots']
        self.assertTrue(service._continuous_pair(a,b,self.result))
        for update in ({'type':'crossfade'}, {'type':'match cut','reason':'hours later'}):
            self.result['assembly']['transitions'][0].update(update)
            self.assertFalse(service._continuous_pair(a,b,self.result))
        self.result['assembly']['transitions'][0].update(type='match cut',reason='shape match')
        b['scene_number']=2
        self.assertFalse(service._continuous_pair(a,b,self.result))
        a['scene_number']=b['scene_number']=None
        self.assertFalse(service._continuous_pair(a,b,self.result))

    def test_continuation_is_separate_real_image_context(self):
        parts=service._continuation_parts((1,self.image),checking=True)
        self.assertIn("Previous shot 1's actual accepted still",parts[0]['text'])
        self.assertEqual(parts[1],service._inline(self.image))
        self.assertIn('Reject clear unexplained state discontinuities',parts[0]['text'])
        self.assertEqual(service._continuation_parts(None),[])

    def test_decoded_ratio_rounding_orientation_and_corruption(self):
        buffer = io.BytesIO()
        service.Image.new("RGB", (1376, 768)).save(buffer, format="JPEG")
        image = GeneratedCharacterImage(buffer.getvalue(), "image/jpeg")
        self.assertTrue(service.check_dimensions(image, "16:9")["matches"])
        self.assertFalse(service.check_dimensions(image, "9:16")["matches"])
        with self.assertRaises(service.StillFrameError):
            service.check_dimensions(GeneratedCharacterImage(b"corrupt", "image/png"), "16:9")

    @patch.object(service.storage_service, "upload_bytes")
    @patch.object(service, "check_still")
    @patch.object(service, "generate_still")
    def test_wrong_ratio_warns_retries_and_never_uploads(self, generate, visual_check, upload):
        generate.return_value = self.image
        self.result["aspect_ratio"] = "16:9"
        self.run_stills()
        self.assertEqual(generate.call_count, 2)
        visual_check.assert_not_called()
        upload.assert_not_called()
        self.assertIsNone(self.result["shots"][0]["still_frame_url"])
        self.assertIn("Aspect-ratio mismatch", self.result["shots"][0]["still_frame_warning"])
        self.assertTrue(any("WARNING:" in c.args[1] and "90x160" in c.args[1] for c in self.emit.call_args_list))

    def test_visual_only_both_dialogue_families(self):
        for suffix in ('Performance reference — Speaker: "Hi." Visual performance only;',
                       'Visual performance only; use audio.\nDialogue:\nSpeaker: "Hi."',
                       'Maintain visual consistency with these reference images — A: https://private.invalid/?sig=secret.'):
            self.assertEqual(service.visual_description("A blue cup. " + suffix), "A blue cup.")

    @patch.object(service, "_google")
    def test_real_provider_approval_with_null_reason(self, google):
        google.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps({"approved": True, "reason": None})}]}}]}
        self.assertTrue(service.check_still("A cup", [], self.image)["approved"])
        google.return_value["candidates"][0]["content"]["parts"][0]["text"] = '{"approved":false,"reason":null}'
        with self.assertRaises(service.StillFrameError):
            service.check_still("A cup", [], self.image)

    @patch.object(service.storage_service, "upload_bytes", return_value={"url": "https://stored", "key": "stored"})
    @patch.object(service, "check_still", side_effect=[{"approved": False, "reason": "Wrong framing"}, {"approved": True, "reason": "Matches"}])
    @patch.object(service, "generate_still")
    def test_visual_rejection_retries_then_persists(self, generate, check, upload):
        generate.return_value = self.image
        shots = self.run_stills()
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args.args[-1], "Wrong framing")
        self.assertEqual(generate.call_args.args[2], "9:16")
        self.assertEqual(shots[0]["still_frame_url"], "https://stored")
        upload.assert_called_once()

    @patch.object(service, "generate_still", side_effect=service.StillFrameError("HTTP 404"))
    def test_provider_failure_continues_and_clears_stale_image(self, generate):
        self.result["shots"][0].update(still_frame_url="old", still_frame_key="old")
        self.result["shots"].append({**self.result["shots"][0], "shot_number": 2})
        shots = self.run_stills()
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(all(s["still_frame_url"] is None and "still_frame_key" not in s for s in shots))
        self.assertTrue(all("Continuing" in s["still_frame_warning"] for s in shots))
        self.assertTrue(any("WARNING:" in c.args[1] for c in self.emit.call_args_list))

    @patch.object(service, "generate_still")
    @patch.object(service, "_download_reference_image", side_effect=RuntimeError("expired"))
    def test_missing_reference_never_silently_generates_new_face(self, download, generate):
        self.result["continuity"]["characters"] = [{"name": "Meera", "character_id": "vault", "image_url": "https://reference"}]
        self.result["shots"][0]["characters_in_shot"] = ["Meera"]
        self.run_stills()
        generate.assert_not_called()
        self.assertIsNone(self.result["shots"][0]["still_frame_url"])

    @patch.object(service, "generate_still")
    def test_uncompiled_dialogue_waits(self, generate):
        self.result["shots"][0].pop("compiled_prompt")
        self.run_stills()
        generate.assert_not_called()

    @patch.object(service.storage_service, "asset_url", return_value="https://renewed")
    def test_preview_url_renewal_and_edit_invalidation(self, renew):
        shot = self.result["shots"][0]
        shot.update(still_frame_key="stored", still_frame_url="expired")
        shot["still_frame_source_hash"] = service.shot_fingerprint(shot)
        job = SimpleNamespace(result_json=json.dumps(self.result))
        self.assertEqual(job_service.job_result(job)["shots"][0]["still_frame_url"], "https://renewed")
        shot["description"] = "A different red bowl"
        job.result_json = json.dumps(self.result)
        self.assertIsNone(job_service.job_result(job)["shots"][0]["still_frame_url"])
        renew.assert_called_once()
        # Revise returns its result directly: invalidation must happen on write,
        # not only on a later GET, so an edited card never displays stale pixels.
        with patch.object(job_service, "get_job", return_value=job):
            job_service.set_result(Mock(), "audit", self.result)
        self.assertIsNone(self.result["shots"][0]["still_frame_url"])
        self.assertIsNone(json.loads(job.result_json)["shots"][0]["still_frame_url"])

    def test_dialogue_hook_compiles_then_generates_stills_after_real_assembly(self):
        from app.agents import director
        order = []
        self.result.update(format={"duration_target_sec": 4}, assembly={"provisional": True})
        self.result["shots"][0].update(has_dialogue=True, status="done")
        job = SimpleNamespace(brief="audit", ai_model="Veo 3.1")
        def assemble(*args, **kwargs):
            order.append("assembly")
            return {"shots": self.result["shots"], "assembly": {"transitions": [], "total_duration_sec": 4}}
        def compile(result, **kwargs):
            order.append("compile")
            self.assertFalse(result["assembly"]["provisional"])
            return result["shots"]
        def stills(result, **kwargs):
            order.append("stills")
            return result["shots"]
        with patch.object(director.job_service, "get_job", return_value=job), patch.object(director.job_service, "job_result", return_value=self.result), patch.object(director.job_service, "append_event"), patch.object(director.job_service, "set_result"), patch.object(director, "assemble_shots", side_effect=assemble), patch.object(director, "compile_shot_prompts", side_effect=compile), patch.object(director, "generate_still_frames", side_effect=stills):
            director.finalize_audio_assembly(Mock(), "audit")
        self.assertEqual(order, ["assembly", "compile", "stills"])

    @patch.object(service.storage_service, "upload_bytes", return_value={"url": "https://stored", "key": "stored"})
    @patch.object(service, "check_still", return_value={"approved": True, "reason": "Matches"})
    @patch.object(service, "generate_still")
    @patch.object(service, "_download_reference_image")
    def test_only_visible_vault_references_condition_generation(self, download, generate, check, upload):
        generate.return_value = download.return_value = self.image
        self.result["continuity"]["characters"] = [
            {"name": "Meera", "character_id": "vault", "image_url": "https://reference"},
            {"name": "Offscreen", "character_id": "other", "image_url": "https://other"}]
        self.result["shots"][0]["characters_in_shot"] = ["Meera"]
        self.run_stills()
        self.assertEqual(generate.call_args.args[1], [("Meera", self.image)])
        download.assert_called_once_with("https://reference")


if __name__ == "__main__":
    unittest.main()
