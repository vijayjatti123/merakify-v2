import copy
import unittest
import json
import threading
import time
from unittest.mock import Mock, patch

from app.services import shot_prompt_compiler as compiler
from app.agents import director, prompts, llm_client


def source(model="Seedance 2.5", dialogue=False):
    return {"ai_model": model, "format": {"format": "ad"},
            "continuity": {"characters": [], "visual_style": {"rendering": "natural", "palette": "blue"}},
            "script": {"scenes": [{"scene_number": 1, "mood": "quiet"}]},
            "assembly": {"transitions": [], "provisional": False},
            "shots": [{"shot_number": 1, "scene_number": 1, "description": "Blue cup", "camera_angle": "wide",
                       "camera_movement": "static", "lens": "normal lens", "lighting": "window light",
                       "composition_note": "centered", "characters_in_shot": [],
                       "has_dialogue": dialogue, "dialogue_text": "Look here." if dialogue else ""}]}


def prose():
    return ("Blue cup occupies the supplied central position, keeping its existing outline clear against the surrounding space. "
            "Render style: natural blue tones with the supplied window light, preserving the established appearance without adding a new surface treatment. "
            "Keep the camera static at the given wide angle; let the existing lighting describe the cup through its visible contours while leaving the surrounding setting as supplied. "
            "Arri Alexa tonal latitude provides a rendering comparison while the composition remains centered, with attention on the supplied object throughout the full duration and ambient sound unspecified. "
            + compiler.TEXT_GUARD)


class CompilerTests(unittest.TestCase):
    def lighting_case(self):
        result = source()
        result['shots'][0]['lighting'] = 'soft diffused overhead, motivated rim highlight'
        result['shots'].append({**result['shots'][0], 'shot_number': 2})
        result['assembly']['transitions'] = [{'between': '1-2', 'type': 'match cut', 'reason': 'continuous motion'}]
        payload = compiler.compiler_input(result, emit=Mock())
        # Real failed job's repeated setup; different actions and wording around it.
        response = {'shots': [
            {'shot_number': 1, 'compiled_prompt': 'Since the liquid is transparent, the key glows from behind and below the glass so amber light transmits outward; the crown rises.'},
            {'shot_number': 2, 'compiled_prompt': 'Because the soda stays transparent, the key glows from behind and below the glass so amber light transmits through the ripples; foam settles.'},
        ]}
        return payload, response

    def test_unchanged_light_setup_can_repeat_but_other_checks_remain(self):
        payload, response = self.lighting_case()
        errors = compiler.validate_compiled(response, payload)
        self.assertFalse(any('repeated descriptive clause' in e for e in errors), errors)
        self.assertTrue(any('early Render style sentence missing' in e for e in errors))
        self.assertTrue(any('word count' in e for e in errors))

    def test_lighting_exception_requires_unchanged_source_scene_and_light(self):
        for changed in ({'scene_number': 2}, {'scene_number': None},
                        {'lighting': 'harsh frontal spotlight'}, {'lighting': ''}):
            with self.subTest(changed=changed):
                payload, response = self.lighting_case()
                payload['shots'][1].update(changed)
                self.assertIn('repeated descriptive clause', str(compiler.validate_compiled(response, payload)))
        payload, response = self.lighting_case()
        payload['boundaries'][0]['reason'] = 'several hours later'
        self.assertIn('repeated descriptive clause', str(compiler.validate_compiled(response, payload)))

    def test_lighting_exception_checks_generated_source_role_position_and_modifiers(self):
        for original, replacement in (
            ('the key', 'the fill'), ('behind and below', 'in front and below'),
            ('the key', 'the warm key'), ('from behind', 'not from behind'),
        ):
            with self.subTest(replacement=replacement):
                payload, response = self.lighting_case()
                response['shots'][1]['compiled_prompt'] = response['shots'][1]['compiled_prompt'].replace(original, replacement)
                self.assertIn('repeated descriptive clause', str(compiler.validate_compiled(response, payload)))

    def test_lighting_exception_does_not_hide_other_repeated_clauses(self):
        for boilerplate in (
            'The pale cup keeps its circular outline against the empty table.',
            'dreamy memories awaken beneath a timeless veil of cinematic beauty',
        ):
            payload, response = self.lighting_case()
            for shot in response['shots']:
                # Append both outside and inside a lighting clause to verify that
                # mentioning a light never exempts an entire decorative sentence.
                shot['compiled_prompt'] = shot['compiled_prompt'].replace(';', ' ' + boilerplate + ';', 1)
            self.assertIn('repeated descriptive clause', str(compiler.validate_compiled(response, payload)))

    def test_light_setup_after_intervening_scene_is_not_exempt(self):
        payload, response = self.lighting_case()
        payload['shots'].insert(1, {**payload['shots'][0], 'shot_number': 9, 'scene_number': 2})
        response['shots'].insert(1, {'shot_number': 9, 'compiled_prompt': 'An unrelated scene.'})
        self.assertIn('repeated descriptive clause', str(compiler.validate_compiled(response, payload)))

    def test_reference_url_is_inserted_without_provider_retyping(self):
        result=source()
        url="https://example.test/meera.png?X-Amz-Date=20260911T184033Z&X-Amz-Expires=3600&signature=unaltered"
        result["continuity"]["characters"]=[{"name":"Meera","gender":"female","image_url":url}]
        result["shots"][0]["characters_in_shot"]=["Meera"]
        payload=compiler.compiler_input(result,emit=Mock())
        raw={"shots":[{"shot_number":1,"compiled_prompt":prose().replace("Blue cup","Meera")}]}
        rendered=compiler.insert_dialogue(raw,payload)
        self.assertIn(compiler.reference_insert(payload["shots"][0]),rendered["shots"][0]["compiled_prompt"])
        self.assertIn(url,rendered["shots"][0]["compiled_prompt"])
        self.assertFalse(any("reference" in e or "image_url" in e for e in compiler.validate_compiled(rendered,payload)))
        call=Mock(return_value=raw)
        compiler.compile_shot_prompts(result,emit=Mock(),call_agent=call)
        sent=json.loads(call.call_args.args[1])
        self.assertNotIn(url,call.call_args.args[1])
        self.assertTrue(sent["shots"][0]["character_references"][0]["has_image_reference"])

    def test_selfie_angle_hyphenation_is_not_a_framing_error(self):
        result=source();result["format"]["format"]="ugc"
        payload=compiler.compiler_input(result,emit=Mock())
        for wording in ("selfie-angle phone framing","selfie angle","arm's-length","handheld-phone framing"):
            value=prose().replace("given wide angle",wording)
            errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":value}]},payload)
            self.assertFalse(any("UGC requires" in e for e in errors),wording)
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":prose()}]},payload)
        self.assertTrue(any("UGC requires" in e for e in errors))

    def test_cup_insert_subject_is_not_overridden_by_cast_tag(self):
        result=source()
        result["continuity"]["characters"]=[{"name":"Meera","gender":"female"}]
        result["shots"][0].update(description="Close on cup resting on table",characters_in_shot=["Meera"])
        payload=compiler.compiler_input(result,emit=Mock())
        self.assertEqual(payload["shots"][0]["subject_anchor"],"cup")
        value=prose().replace("Blue cup","Opening on that same blue ceramic cup")
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":value}]},payload)
        self.assertFalse(any("subject_anchor" in e for e in errors))
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":value.replace("cup","vase")}]},payload)
        self.assertTrue(any("subject_anchor" in e for e in errors))

    def test_subject_check_accepts_grammatical_expansion_not_wrong_subject(self):
        for description,opening,anchor in (("Macro reveals glowing curved steel edge", "Reveals the glowing curved steel edge", "glowing curved steel edge"),
                                           ("Close-up reveals clear bottle, blue lid", "A clear water bottle", "clear bottle"),
                                           ("Close insert of blue screw lid.", "A close insert opens on the blue screw-on lid's cap edge", "blue screw lid")):
            result=source();result["shots"][0]["description"]=description
            payload=compiler.compiler_input(result,emit=Mock())
            self.assertEqual(payload["shots"][0]["subject_anchor"],anchor)
            errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":prose().replace("Blue cup",opening)}]},payload)
            self.assertFalse(any("subject_anchor" in e for e in errors))
            errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":prose()}]},payload)
            self.assertTrue(any("subject_anchor" in e for e in errors))

    def test_offscreen_dialogue_keeps_prior_speaker_across_batches(self):
        result = source(dialogue=True)
        result["continuity"]["characters"] = [{"name": "Speaker", "gender": "unspecified"}]
        result["shots"] = [dict(result["shots"][0], shot_number=n,
                                characters_in_shot=["Speaker"] if n == 1 else []) for n in range(1, 6)]
        result["assembly"]["transitions"] = [{"between": f"{n}-{n+1}", "type": "cut"} for n in range(1, 5)]
        payload = compiler.compiler_input(result, emit=Mock())
        self.assertEqual([s["speaker_label"] for s in payload["shots"]], ["Speaker"] * 5)
        self.assertEqual(payload["shots"][2]["characters_in_shot"], [])
        self.assertEqual(payload["shots"][2]["speaker_reference"]["gender"], "unspecified")
        self.assertEqual(payload["shots"][0]["character_references"][0]["gender"], "unspecified")
        result["shots"][0]["has_dialogue"] = False
        self.assertEqual(compiler.compiler_input(result, emit=Mock())["shots"][2]["speaker_label"], "Narrator")

    def test_unknown_gender_rejects_pronouns_but_not_source_dialogue(self):
        for gender in (None, "unspecified", "", "unknown", "nonbinary", 42):
            result = source(dialogue=True)
            result["continuity"]["characters"] = [{"name": "Speaker", "gender": gender}]
            result["shots"][0].update(characters_in_shot=["Speaker"], dialogue_text="She said hello.")
            payload = compiler.compiler_input(result, emit=Mock())
            self.assertTrue(payload["shots"][0]["neutral_pronouns_required"])
            for word, rejected in (("her", True), ("she", True), ("his", True), ("they", False), ("the speaker", False)):
                response = compiler.insert_dialogue({"shots": [{"shot_number": 1,
                    "compiled_prompt": prose().replace("Blue cup", "Speaker").replace("its existing", word + " existing")}]}, payload)
                errors = compiler.validate_compiled(response, payload)
                self.assertEqual(any("unsupported gendered pronoun" in e for e in errors), rejected)

    def test_known_gender_still_allows_pronouns(self):
        result = source()
        result["continuity"]["characters"] = [{"name": "Meera", "gender": "female"}]
        result["shots"][0]["characters_in_shot"] = ["Meera"]
        payload = compiler.compiler_input(result, emit=Mock())
        self.assertFalse(payload["shots"][0]["neutral_pronouns_required"])
        errors = compiler.validate_compiled({"shots": [{"shot_number": 1,
            "compiled_prompt": prose().replace("Blue cup", "Meera").replace("its existing", "her existing")}]}, payload)
        self.assertFalse(any("unsupported gendered pronoun" in e for e in errors))

    def test_valid_compile_adds_only_prompt_and_uses_existing_model_call(self):
        result = source(); original = copy.deepcopy(result)
        call = Mock(return_value={"shots": [{"shot_number": 1, "compiled_prompt": prose()}]})
        compiled = compiler.compile_shot_prompts(result, emit=Mock(), call_agent=call)
        self.assertEqual(result, original)
        self.assertEqual({k:v for k,v in compiled[0].items() if k != "compiled_prompt"}, original["shots"][0])
        self.assertEqual(call.call_args.args[0], prompts.SHOT_PROMPT_COMPILER)
        self.assertNotIn("fast", call.call_args.kwargs)

    def test_bad_output_retries_once_then_fails_visibly(self):
        call = Mock(return_value={"shots": [{"shot_number": 1, "compiled_prompt": "Too short"}]})
        emit = Mock()
        with self.assertRaisesRegex(ValueError, "failed validation"):
            compiler.compile_shot_prompts(source(), emit=emit, call_agent=call)
        self.assertEqual(call.call_count, 2)
        self.assertIn("required_corrections", call.call_args.args[1])

    def test_no_compilation_from_provisional_assembly(self):
        result=source();result["assembly"]["provisional"]=True
        call=Mock()
        with self.assertRaisesRegex(ValueError,"real, completed Assembly"):
            compiler.compile_shot_prompts(result,emit=Mock(),call_agent=call)
        call.assert_not_called()

    def test_subject_anchor_does_not_require_a_terminal_period_inside_prose(self):
        result = source(); result["shots"][0]["description"] = "Blue cup."
        payload = compiler.compiler_input(result, emit=Mock())
        self.assertEqual(payload["shots"][0]["subject_anchor"], "Blue cup")
        self.assertEqual(result["shots"][0]["description"], "Blue cup.")

    def test_compound_movement_only_reduced_in_compiler_input(self):
        result=source();result["shots"][0]["camera_movement"]="slow push-in then pan"
        emit=Mock();payload=compiler.compiler_input(result,emit=emit)
        self.assertEqual(payload["shots"][0]["camera_movement"],"slow push-in")
        self.assertEqual(result["shots"][0]["camera_movement"],"slow push-in then pan")
        self.assertTrue(any("compound movement" in call.args[1] for call in emit.call_args_list))
        self.assertEqual(compiler.first_movement("slow static push"), ("static", True))

    def test_model_families_and_dialogue_guards(self):
        for model,family in [("Veo 3.1","veo"),("Sora 2","sora"),("Kling 3.0","kling"),("Seedance 2.5","generic"),("Wan 2.5","generic")]:
            self.assertEqual(compiler.model_family(model),family)
        payload=compiler.compiler_input(source("Sora 2",True),emit=Mock())
        rendered=compiler.insert_dialogue({"shots":[{"shot_number":1,"compiled_prompt":prose()}]},payload)
        self.assertTrue(rendered["shots"][0]["compiled_prompt"].endswith('\nDialogue:\nNarrator: "Look here."'))

    def test_vault_identity_and_anime_guards(self):
        result=source()
        result["continuity"]["characters"]=[{"name":"Maya","description":"Locked description","image_url":"https://example.test/vault.png","voice_id":"priya","character_id":"vault-1"}]
        result["shots"][0]["characters_in_shot"]=["Maya"]
        result["continuity"]["visual_style"]["rendering"]="2D anime cel-shaded"
        payload=compiler.compiler_input(result,emit=Mock())
        self.assertEqual(payload["shots"][0]["category"],"anime")
        self.assertIsNone(payload["shots"][0]["hardware_language"])
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":prose()}]},payload)
        self.assertIn("locked image_url",str(errors));self.assertIn("anime",str(errors))

    def test_all_neighbor_context_and_real_boundaries_required(self):
        result=source();result["shots"].append({**result["shots"][0],"shot_number":2,"camera_angle":"wide eye-level"})
        with self.assertRaisesRegex(ValueError,"missing Assembler boundary"):
            compiler.compiler_input(result,emit=Mock())
        result["assembly"]["transitions"]=[{"between":"1-2","type":"cut","reason":"continue"}]
        payload=compiler.compiler_input(result,emit=Mock())
        self.assertTrue(payload["boundaries"][0]["similar_framing_risk"])
        result["assembly"]["transitions"][0]["type"]="match cut"
        payload=compiler.compiler_input(result,emit=Mock())
        errors=compiler.validate_compiled({"shots":[{"shot_number":n,"compiled_prompt":prose()} for n in (1,2)]},payload)
        self.assertIn("coordinate left ending and right opening",str(errors))

    def test_negated_anime_terms_do_not_select_anime_register(self):
        result = source()
        result["continuity"]["visual_style"]["rendering"] = "Natural live-action, no stylization or cel-shading"
        payload = compiler.compiler_input(result, emit=Mock())
        self.assertEqual(payload["shots"][0]["category"], "product")
        result["continuity"]["visual_style"]["rendering"] = "Cel-shaded anime; no photorealism"
        self.assertEqual(compiler.compiler_input(result, emit=Mock())["shots"][0]["category"], "anime")

    def test_register_is_per_shot_and_product_link_is_not_invented(self):
        result=source();result["shots"][0]["description"]="Cup on desk"
        result["shots"].append({**result["shots"][0],"shot_number":2,"has_dialogue":True,"dialogue_text":"Look here."})
        result["assembly"]["transitions"]=[{"between":"1-2","type":"cut"}]
        payload=compiler.compiler_input(result,emit=Mock())
        self.assertEqual([s["category"] for s in payload["shots"]],["product","character_dialogue"])
        self.assertEqual(payload["shots"][0]["tagged_product_references"],[])

    def test_compiler_failure_does_not_relabel_successful_audio_assembly(self):
        result=source();result["shots"][0].update(has_dialogue=True,status="done")
        job=Mock(ai_model="Veo 3.1",brief="test")
        with patch.object(director.job_service,"get_job",return_value=job), patch.object(director.job_service,"job_result",return_value=result), patch.object(director.job_service,"append_event"), patch.object(director.job_service,"set_status") as status, patch.object(director.job_service,"set_result") as save, patch.object(director,"assemble_shots",return_value={"shots":result["shots"],"assembly":{"transitions":[],"total_duration_sec":3}}), patch.object(director,"compile_shot_prompts",side_effect=ValueError("compiler only")):
            result["format"]["duration_target_sec"]=3
            director.finalize_audio_assembly(Mock(),"job")
        self.assertEqual(status.call_args.args[2],"error")
        self.assertFalse(save.call_args.args[2]["assembly"]["provisional"])
        self.assertFalse(save.call_args.args[2]["audio_assembly_pending"])

    def test_revision_hardware_and_ugc_are_enforced(self):
        payload=compiler.compiler_input(source(),emit=Mock())
        self.assertEqual(payload["shots"][0]["hardware_language"],"Arri Alexa tonal latitude")
        text=prose().replace("Arri Alexa tonal latitude","Ordinary tonal range")
        self.assertIn("hardware_language",str(compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":text}]},payload)))
        payload=compiler.compiler_input(source(),brief="Content type: UGC.",emit=Mock())
        self.assertIsNone(payload["shots"][0]["hardware_language"])
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":prose()+" eye-level medium shot"}]},payload)
        self.assertIn("UGC conventional framing",str(errors))
        self.assertIn("UGC requires",str(errors))

    def test_revision_rejects_pasted_identity_and_repeated_clauses(self):
        result=source();result["continuity"]["characters"]=[{"name":"Meera","description":"Woman wearing a red jacket and blue trousers"}]
        result["shots"][0]["characters_in_shot"]=["Meera"]
        payload=compiler.compiler_input(result,emit=Mock())
        text="Meera, Woman wearing a red jacket and blue trousers. "+prose()
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":text}]},payload)
        self.assertIn("stored description pasted",str(errors))
        result=source();result["shots"].append({**result["shots"][0],"shot_number":2})
        result["assembly"]["transitions"]=[{"between":"1-2","type":"cut"}]
        payload=compiler.compiler_input(result,emit=Mock())
        errors=compiler.validate_compiled({"shots":[{"shot_number":n,"compiled_prompt":prose()} for n in (1,2)]},payload)
        self.assertIn("repeated descriptive clause",str(errors))

    def test_grammatical_camera_wording_preserves_direction(self):
        self.assertTrue(compiler.movement_present("slow push-in", "The camera pushes in slowly toward the cup"))
        self.assertFalse(compiler.movement_present("slow push-in", "The camera pulls out slowly from the cup"))
        result=source();result["continuity"]["visual_style"]["rendering"]="cel-shaded anime"
        result["shots"][0]["camera_movement"]="quick handheld shake"
        self.assertEqual(compiler.compiler_input(result,emit=Mock())["shots"][0]["camera_movement"],"static")
        self.assertEqual(result["shots"][0]["camera_movement"],"quick handheld shake")

    def test_revision_keeps_sora_dialogue_separate_and_subject_placement(self):
        payload=compiler.compiler_input(source("Sora 2",True),emit=Mock())
        value=prose()+compiler.AUDIO_GUARD+'\nDialogue:\nNarrator: "Look here."'
        errors=compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":value}]},payload)
        self.assertNotIn("Sora:",str(errors))
        result=source();result["continuity"]["characters"]=[{"name":"Meera","description":"red jacket"}]
        result["shots"][0].update(characters_in_shot=["Meera"],composition_note="Meera left, window backlight")
        payload=compiler.compiler_input(result,emit=Mock())
        self.assertIn("left composition",str(compiler.validate_compiled({"shots":[{"shot_number":1,"compiled_prompt":"Meera. "+prose()}]},payload)))

    def test_dialogue_is_inserted_from_source_and_absent_from_model_request(self):
        for model in ("Veo 3.1", "Sora 2", "Kling 3.0", "Seedance 2.5"):
            result=source(model,True)
            line='रुको—यहाँ देखो।  Don’t change "this"!'
            result['shots'][0]['dialogue_text']=line
            original=copy.deepcopy(result)
            visual=prose().replace('Arri Alexa tonal latitude','35mm f/1.4 intimate optical separation')
            call=Mock(return_value={'shots':[{'shot_number':1,'compiled_prompt':visual}]})
            compiled=compiler.compile_shot_prompts(result,emit=Mock(),call_agent=call)
            self.assertIn('"'+line+'"',compiled[0]['compiled_prompt'])
            self.assertEqual(result,original)
            request=json.loads(call.call_args.args[1])
            self.assertNotIn('dialogue_text',request['shots'][0])
            self.assertNotIn(line,call.call_args.args[1])
            self.assertEqual(compiled[0]['compiled_prompt'].count(compiler.AUDIO_GUARD),1)

    def test_deadline_returns_without_waiting_for_late_provider_or_persisting_it(self):
        release=threading.Event(); finished=threading.Event()
        result=source();original=copy.deepcopy(result);emit=Mock()
        def blocked(*args,**kwargs):
            release.wait(2)
            finished.set()
            return {'shots':[{'shot_number':1,'compiled_prompt':prose()}]}
        start=time.monotonic()
        try:
            with patch.object(compiler,'COMPILER_DEADLINE_SEC',0.04):
                with self.assertRaises(TimeoutError):
                    compiler.compile_shot_prompts(result,emit=emit,call_agent=blocked)
            self.assertLess(time.monotonic()-start,0.5)
            self.assertIn('"error_type": "TimeoutError"',str(emit.call_args_list))
        finally:
            release.set();finished.wait(1)
        self.assertEqual(result,original)

    def test_timing_logs_cover_both_attempts_and_provider_error(self):
        emit=Mock();call=Mock(side_effect=[{'shots':[]},RuntimeError('provider failed')])
        with self.assertRaisesRegex(RuntimeError,'provider failed'):
            compiler.compile_shot_prompts(source(),emit=emit,call_agent=call)
        logs=[json.loads(c.args[1].split(': ',1)[1]) for c in emit.call_args_list if c.args[1].startswith('Compiler attempt timing: ')]
        self.assertEqual([(x['attempt'],x['phase']) for x in logs],[(1,'start'),(1,'end'),(2,'start'),(2,'end')])
        self.assertEqual(logs[1]['outcome'],'validation_rejected')
        self.assertEqual(logs[-1]['error_type'],'RuntimeError')

    def test_corrective_attempt_shares_original_deadline(self):
        def slow_invalid(*args,**kwargs):
            time.sleep(0.06)
            return {'shots':[]}
        emit=Mock();started=time.monotonic()
        with patch.object(compiler,'COMPILER_DEADLINE_SEC',0.10):
            with self.assertRaises(TimeoutError):
                compiler.compile_shot_prompts(source(),emit=emit,call_agent=slow_invalid)
        self.assertLess(time.monotonic()-started,0.18)
        logs=[json.loads(c.args[1].split(': ',1)[1]) for c in emit.call_args_list if c.args[1].startswith('Compiler attempt timing: ')]
        self.assertEqual(logs[-1]['attempt'],2)
        self.assertEqual(logs[-1]['error_type'],'TimeoutError')

    def test_transport_budget_is_opt_in_without_changing_existing_agents(self):
        response=Mock(stop_reason='end_turn',content=[Mock(type='text',text='{}')])
        with patch.object(llm_client,'_client') as client:
            client.messages.create.return_value=response
            client.with_options.return_value.messages.create.return_value=response
            self.assertEqual(llm_client.call_agent('system','input'),{})
            client.with_options.assert_not_called()
            self.assertEqual(llm_client.call_agent('system','input',request_timeout=123.0),{})
            client.with_options.assert_called_once_with(timeout=123.0,max_retries=0)
            self.assertEqual(client.messages.create.call_count,1)

    def test_actual_meera_downward_wording_and_missing_source_movement(self):
        self.assertTrue(compiler.movement_present('slow tilt down','The phone framing tilts slowly downward'))
        self.assertFalse(compiler.movement_present('slow tilt down','The phone framing tilts slowly upward'))
        self.assertFalse(compiler.movement_present('slow tilt down','The phone framing tilts downward'))
        for missing in (None, '', '   '):
            result=source();result['shots'][0]['camera_movement']=missing
            emit=Mock();payload=compiler.compiler_input(result,emit=emit)
            self.assertEqual(payload['shots'][0]['camera_movement'],'static')
            self.assertTrue(compiler.movement_present(payload['shots'][0]['camera_movement'],'Hold the camera static'))
            self.assertIn('source camera_movement missing',str(emit.call_args_list))

    def test_hardware_identity_can_repeat_but_technical_tag_is_checked(self):
        result=source();result['shots'].append({**result['shots'][0],'shot_number':2})
        result['assembly']['transitions']=[{'between':'1-2','type':'cut'}]
        payload=compiler.compiler_input(result,emit=Mock())
        response={'shots':[{'shot_number':n,'compiled_prompt':prose()} for n in (1,2)]}
        errors=compiler.validate_compiled(response,payload)
        self.assertIn('repeated hardware phrasing: Arri Alexa tonal latitude',str(errors))
        response['shots'][1]['compiled_prompt']=prose().replace('Arri Alexa tonal latitude','Arri Alexa shadow separation')
        self.assertNotIn('repeated hardware phrasing',str(compiler.validate_compiled(response,payload)))
        self.assertNotIn('hardware_language reference missing',str(compiler.validate_compiled(response,payload)))

    def test_batch_partition_keeps_simple_match_pair_together(self):
        result=source();result['shots']=[{**result['shots'][0],'shot_number':n} for n in range(1,9)]
        result['assembly']['transitions']=[{'between':f'{n}-{n+1}','type':'match cut' if n==4 else 'cut'} for n in range(1,8)]
        batches=list(compiler.shot_batches(compiler.compiler_input(result,emit=Mock())))
        self.assertTrue(all(len(b)<=4 for b in batches))
        self.assertTrue(any({4,5}<={s['shot_number'] for s in b} for b in batches))
        self.assertEqual([s['shot_number'] for b in batches for s in b],list(range(1,9)))

    def test_repetition_across_batches_is_rejected_without_partial_result(self):
        result=source();result['shots'].append({**result['shots'][0],'shot_number':2})
        result['assembly']['transitions']=[{'between':'1-2','type':'cut'}]
        original=copy.deepcopy(result)
        def answer(system, content, **kwargs):
            data=json.loads(content);data=data.get('input',data)
            return {'shots':[{'shot_number':data['shots'][0]['shot_number'],'compiled_prompt':prose()}]}
        call=Mock(side_effect=answer)
        with patch.object(compiler,'COMPILER_BATCH_SIZE',1):
            with self.assertRaisesRegex(ValueError,'repeated'):
                compiler.compile_shot_prompts(result,emit=Mock(),call_agent=call)
        self.assertEqual(result,original)
        second=json.loads(call.call_args_list[1].args[1])
        self.assertEqual([s['shot_number'] for s in second['shots']],[2])
        self.assertEqual(second['prior_compiled_shots'],[])
        correction=json.loads(call.call_args_list[-1].args[1])['input']
        self.assertEqual(correction['prior_compiled_shots'][0]['compiled_prompt'],prose())
        self.assertEqual(second['readonly_neighbors'][0]['shot_number'],1)

    def test_malformed_provider_json_uses_only_existing_retry(self):
        call=Mock(side_effect=[ValueError('Model response was not valid JSON (bad delimiter)'),
                              {'shots':[{'shot_number':1,'compiled_prompt':prose()}]}])
        emit=Mock()
        self.assertEqual(len(compiler.compile_shot_prompts(source(),emit=emit,call_agent=call)),1)
        self.assertEqual(call.call_count,2)
        self.assertIn('"outcome": "format_rejected"',str(emit.call_args_list))
        call=Mock(side_effect=ValueError('Model response was cut off at token limit'))
        with self.assertRaisesRegex(ValueError,'cut off'):
            compiler.compile_shot_prompts(source(),emit=Mock(),call_agent=call)
        self.assertEqual(call.call_count,2)

    def test_batch_lookahead_overlaps_only_provider_work(self):
        result=source();result['shots'].append({**result['shots'][0],'shot_number':2})
        result['assembly']['transitions']=[{'between':'1-2','type':'cut'}]
        rendezvous=threading.Barrier(2)
        def answer(system,content,**kwargs):
            data=json.loads(content)
            rendezvous.wait(timeout=0.5)
            return {'shots':[{'shot_number':data['shots'][0]['shot_number'],'compiled_prompt':prose()}]}
        with patch.object(compiler,'COMPILER_BATCH_SIZE',1), patch.object(compiler,'COMPILER_DEADLINE_SEC',1), patch.object(compiler,'validate_compiled',return_value=[]) as validate:
            compiled=compiler.compile_shot_prompts(result,emit=Mock(),call_agent=answer)
        self.assertEqual([s['shot_number'] for s in compiled],[1,2])
        self.assertEqual([s['shot_number'] for s in validate.call_args.args[0]['shots']],[1,2])
