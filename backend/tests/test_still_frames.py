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
        # Original-generation tests count original uploads; derivatives have
        # separate byte-level/integration coverage in test_preparation_latency.
        display = patch('app.services.preview_display.store_variants', return_value=None)
        display.start()
        self.addCleanup(display.stop)
        self.result = {"aspect_ratio": "9:16", "continuity": {"characters": []}, "shots": [
            {"shot_number": 1, "compiled_prompt": "A blue cup on a table. No on-screen text, logos or readable signage; composite text in post.",
             "characters_in_shot": []}]}
        buffer = io.BytesIO()
        service.Image.new("RGB", (90, 160)).save(buffer, format="PNG")
        self.image = GeneratedCharacterImage(buffer.getvalue(), "image/png")
        self.emit = Mock()

    def test_verification_outage_reuses_saved_candidate_without_generation(self):
        with patch.object(service, 'generate_still', return_value=self.image) as generate, \
             patch.object(service, 'check_still', side_effect=service.VerificationUnavailable('offline')), \
             patch.object(service.storage_service, 'upload_bytes', return_value={'key':'candidate','url':'https://stored'}):
            self.run_stills()
        shot = self.result['shots'][0]
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(shot['still_frame_error_kind'], 'verification')
        self.assertIsNone(shot['still_frame_url'])
        self.assertIn('still_frame_candidate', shot)
        with patch.object(service, 'generate_still') as generate, \
             patch.object(service, '_download_reference_image', return_value=self.image), \
             patch.object(service.storage_service, 'asset_url', return_value='https://candidate'), \
             patch.object(service.storage_service, 'upload_bytes', return_value={'key':'approved','url':'https://approved'}), \
             patch.object(service, 'check_still', return_value={'approved':True,'reason':'Matches'}):
            self.run_stills()
        generate.assert_not_called()
        self.assertEqual(shot['still_frame_status'], 'ready')
        self.assertNotIn('still_frame_candidate', shot)

    def test_opening_prompt_excludes_later_composition(self):
        from app.services.preview_plan import preview_visual
        text = preview_visual({'state_at_shot_start':'Kabir in freefall, pilot chute trailing.',
            'composition_note':'Open main canopy fills frame', 'description':'Parachute deploys',
            'characters':[], 'direction_version':1, 'camera_angle':'High angle'})
        self.assertIn('pilot chute', text)
        self.assertNotIn('Open main canopy', text)
        self.assertNotIn('Parachute deploys', text)
        self.assertIn('High angle', text)

    def test_changed_candidate_source_generates_and_saves_current_evidence(self):
        shot = self.result['shots'][0]
        shot['still_frame_candidate'] = {'key': 'old', 'source': 'stale', 'attempt': 0}
        verdict = self.checked_verdict()
        with patch.object(service, 'generate_still', return_value=self.image) as generate, \
             patch.object(service, 'check_still', return_value=verdict), \
             patch.object(service.storage_service, 'upload_bytes', return_value={'key':'approved','url':'https://approved'}):
            self.run_stills()
        generate.assert_called_once()
        self.assertEqual(shot['still_frame_status'], 'ready')
        self.assertEqual(shot['still_frame_verification'], verdict)
        self.assertNotIn('still_frame_candidate', shot)

    def test_checker_requests_text_model_and_schema(self):
        with patch.object(service.settings, 'google_ai_api_key', 'test'), \
             patch.object(service, 'urlopen') as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'{}'
            service._google([{'text':'Check'}], verification=True)
        request = opened.call_args.args[0]
        self.assertIn(service.settings.gemini_preview_check_model, request.full_url)
        config = json.loads(request.data)['generationConfig']
        self.assertEqual(config['responseModalities'], ['TEXT'])
        self.assertEqual(config['responseSchema']['required'], ['approved','reason','visible_entities','spatially_grounded','visual_checks'])
        self.assertEqual(config['responseSchema']['properties']['visual_checks']['required'], list(service.VISUAL_CHECKS))

    def checked_verdict(self):
        return {'approved': True, 'reason': 'Matches', 'spatially_grounded': True,
                'visible_entities': [], 'visual_checks': {key: {
                    'status': 'pass', 'requirement': 'Supplied opening requirement',
                    'evidence': 'Visible evidence in candidate'} for key in service.VISUAL_CHECKS}}

    def test_uncertain_placement_overrides_approval_and_collects_other_failures(self):
        verdict = self.checked_verdict()
        verdict['visual_checks']['placement_support'].update(status='uncertain', evidence='Cabin floor and threshold are obscured')
        verdict['visual_checks']['opening_state'].update(status='fail', evidence='Main canopy already open')
        response = {'candidates': [{'content': {'parts': [{'text': json.dumps(verdict)}]}}]}
        with patch.object(service, '_google', return_value=response):
            result = service.check_still('Inside helicopter, canopy packed', [], self.image)
        self.assertFalse(result['approved'])
        self.assertFalse(result['spatially_grounded'])
        self.assertIn('Cabin floor', result['reason'])
        self.assertIn('Main canopy', result['reason'])

    def test_missing_checklist_is_verification_outage_not_image_rejection(self):
        verdict = self.checked_verdict()
        del verdict['visual_checks']['placement_support']
        response = {'candidates': [{'content': {'parts': [{'text': json.dumps(verdict)}]}}]}
        with patch.object(service, '_google', return_value=response) as call:
            with self.assertRaises(service.VerificationUnavailable):
                service.check_still('Inside helicopter', [], self.image)
        self.assertEqual(call.call_count, 2)

    def test_helicopter_opening_requires_interior_spatial_grounding(self):
        from app.services.preview_plan import preview_visual
        text = preview_visual({
            'state_at_shot_start': 'Kabir grips the helicopter doorframe inside the cabin.',
            'description': 'Tara hands him a can inside the helicopter.',
            'characters': [], 'camera_angle': 'Eye-level medium',
        })
        self.assertIn('spatial requirements are hard acceptance criteria', text.casefold())
        self.assertIn('support surface and contact relationships', text)
        self.assertNotIn('every listed person must be visibly inside', text)

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
            shot['compiled_prompt'] = f"Audit frame {shot['shot_number']}. " + shot['compiled_prompt']
        check.return_value['visible_entities'] = ['props:cup']
        order = []
        def generate(visual, refs, aspect, feedback, *, continuation, emit):
            import re
            number = int(re.search(r'Audit frame (\d+)', visual).group(1))
            order.append(f'generate{number}')
            self.assertEqual(refs[0][:2], ('Meera', self.image))
            if number > 1:
                self.assertTrue(any('Job entity props:cup' in ref[0] for ref in refs))
            if number in (2, 4):
                self.assertEqual(continuation[:2], (number - 1, self.image))
                self.assertIn(f'upload{number-1}', order)
            else:
                self.assertIsNone(continuation)
            return self.image
        def upload(**kwargs):
            number = int(kwargs['key'].split('/stills/')[1].split('-')[0])
            order.append(f'upload{number}')
            return {'url': f'https://stored/{number}', 'key': str(number)}
        with patch.object(service, '_download_reference_image', return_value=self.image), \
             patch.object(service, 'generate_still', side_effect=generate), \
             patch.object(service.storage_service, 'upload_bytes', side_effect=upload):
            self.run_stills()
        self.assertEqual(set(order), {f'{phase}{n}' for phase in ('generate', 'upload') for n in range(1, 5)})
        for n in range(1, 5): self.assertLess(order.index(f'generate{n}'), order.index(f'upload{n}'))
        for predecessor, successor in ((1, 2), (1, 3), (3, 4)):
            self.assertLess(order.index(f'upload{predecessor}'), order.index(f'generate{successor}'))
        continuations = [c.kwargs['continuation'][:2] for c in check.call_args_list if c.kwargs['continuation']]
        self.assertCountEqual(continuations, [(1, self.image), (3, self.image)])

    @patch.object(service.storage_service, 'upload_bytes', return_value={'url': 'https://stored', 'key': 'stored'})
    @patch.object(service, 'check_still', return_value={'approved': True, 'reason': 'Matches'})
    def test_failed_previous_shot_breaks_chain_without_blocking_next(self, check, upload):
        self.continuous_shots([1,1,1])
        calls = []
        def generate(*args, continuation=None, emit=None):
            calls.append(continuation[:2] if continuation else None)
            if len(calls) == 2:
                raise service.StillFrameError('forced failure')
            return self.image
        with patch.object(service, 'generate_still', side_effect=generate), patch.object(service, '_download_reference_image', return_value=self.image):
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
        google.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps({**self.checked_verdict(), "reason": None})}]}}]}
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
        self.assertTrue(all("couldn't be generated" in s["still_frame_warning"] for s in shots))
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
        job = SimpleNamespace(result_json=json.dumps(self.result), video_model=None)
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

    def test_dialogue_hook_generates_stills_before_video_compilation_after_real_assembly(self):
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
        self.assertEqual(order[0], "assembly")
        self.assertCountEqual(order[1:], ["stills", "compile"])

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
        self.assertEqual([ref[:2] for ref in generate.call_args.args[1]], [("Meera", self.image)])
        download.assert_called_once_with("https://reference")


if __name__ == "__main__":
    unittest.main()
