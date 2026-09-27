import copy
import json
import unittest
from unittest.mock import patch

from app.agents import director, prompts
from app.services import ad_direction, preview_plan, shot_prompt_compiler
from app.services.planning_patch import apply_patch_response, patch_permissions
from test_shot_prompt_compiler import source


def directed():
    result = source('Seedance 2.0', dialogue=True)
    result['ad_direction'] = dict(takeaway='A quiet tea break', visual_approach='Natural window light',
        pacing='Pause, reach, relief', sound_direction='Quiet piano in the final edit, under speech')
    shot = result['shots'][0]
    shot.update(direction_version=1, duration_sec=5, speech_mode='voiceover', opening_characters=[],
        description='The hand lifts the blue cup', state_at_shot_start='Blue cup on table, hand beside it.',
        state_at_shot_end='Blue cup held above table.', shot_direction=dict(purpose='Anticipate relief',
        performance='An unhurried hand lifts the cup', product_props='Blue cup rests on table',
        edit_intent='Hold for the reaction'))
    ad_direction.accept_shots(result['shots'])
    return result


class AdDirectionTests(unittest.TestCase):
    def test_silent_direction_rejects_vocal_actions(self):
        shot = directed()['shots'][0]
        shot.update(has_dialogue=False, speech_mode='none', dialogue_text='')
        shot['shot_direction']['performance'] = 'He gasps and smiles at the bucket.'
        self.assertIn('gasps', ad_direction.silent_vocal_directions(shot))
        self.assertTrue(any('Silent shot directs vocal performance' in issue
                            for issue in ad_direction.problems(shot)))
        shot['shot_direction']['performance'] = 'He smiles without speaking.'
        self.assertEqual(ad_direction.silent_vocal_directions(shot), [])

    def test_source_required_nonverbal_groan_is_preserved_without_invented_gasp(self):
        shot = directed()['shots'][0]
        shot.update(has_dialogue=False, speech_mode='none', dialogue_text='')
        shot['shot_direction']['performance'] = 'Yamaraj groans, then the carpenter gasps.'
        story = {'production_context': {'original_brief': 'Yamaraj drops his lasso and groans.'}}
        self.assertEqual(ad_direction.silent_vocal_directions(shot, story), ['gasps'])

    def test_invented_silent_gasp_becomes_visible_startle_before_review(self):
        shot = directed()['shots'][0]
        shot.update(has_dialogue=False, speech_mode='none', dialogue_text='',
                    description='The carpenter gasps and jolts upright.')
        shot['shot_direction']['performance'] = 'He gasps, then smiles.'
        story = {'production_context': {'original_brief': 'He wakes with a start and smiles.'}}
        ad_direction.normalize_silent_startle([shot], story)
        self.assertIn('startles and jolts', shot['description'])
        self.assertEqual(ad_direction.silent_vocal_directions(shot, story), [])

    def test_preview_keeps_source_requested_nonverbal_groan(self):
        result = directed()
        result['script'] = {'production_context': {'original_brief': 'Yamaraj drops the lasso and groans.'}}
        shot = result['shots'][0]
        shot.update(has_dialogue=False, speech_mode='none', dialogue_text='')
        shot['shot_direction']['performance'] = 'Yamaraj groans in defeat.'
        shot['direction_source'] = ad_direction.source_key(shot)
        self.assertIsNotNone(preview_plan.preview_input(result, shot))

    def test_future_action_is_checker_context_not_image_generation_text(self):
        facts = {'state_at_shot_start': 'Hand rests beside cup on table.',
                 'camera_angle': 'eye-level close-up', 'lighting': 'Soft window light',
                 'characters': [], 'later_action_beats': ['Hand approaches cup', 'Golden lasso appears later']}
        contract = preview_plan.visual_contract(facts)
        self.assertIn('Golden lasso appears later', preview_plan.contract_text(contract))
        self.assertNotIn('Golden lasso appears later',
            preview_plan.contract_text(contract, for_generation=True))
        self.assertNotIn('Golden lasso appears later', preview_plan.generation_prompt(contract))

    def test_simple_description_edit_refreshes_only_that_shot(self):
        direction = dict(purpose='Show the cup', performance='A hand moves calmly',
            product_props='Blue cup', edit_intent='Hold on the cup',
            blocking='Cup stays on the table.',
            action_beats=['Hand approaches the cup.', 'Hand lifts the cup.'],
            dialogue_beat_index=0, critical_outcome='Cup is lifted visibly.',
            entry_exit_paths=['Hand: frame edge -> table -> cup'],
            support_and_contact='Cup starts on the table and ends in the hand.',
            spatial_invariants=['Cup stays in the room.'], forbidden_geometry=['Cup never floats.'])
        shot = dict(shot_number=1, scene_number=1, description='A hand lifts a blue cup.',
            dialogue_text='', has_dialogue=False, speech_mode='none', speaker_name='',
            characters_in_shot=[], opening_characters=[], duration_sec=5,
            camera_angle='eye-level close-up', camera_direction=dict(movement='hold',
                direction='none', speed='none', stabilization='locked'), lens='50mm',
            lighting='Soft window light', composition_note='Cup centered',
            state_at_shot_start='Cup on table, hand beside it.',
            state_at_shot_end='Cup in hand above table.', transition_after='cut',
            direction_version=1, shot_direction=direction)
        sibling = {**shot, 'shot_number': 2, 'description': 'A woman smiles.',
                   'state_at_shot_start': 'Woman sits indoors.',
                   'state_at_shot_end': 'Woman smiles indoors.'}
        revised = {**shot, 'description': 'The hand sets the cup down gently.'}
        refreshed = {**direction, 'action_beats': ['Hand holds the cup.', 'Hand sets the cup on the table.'],
            'critical_outcome': 'Cup rests on the table.',
            'support_and_contact': 'Hand supports the cup until it rests on the table.'}
        changes = {'shot_direction': refreshed, 'state_at_shot_start': 'Cup in hand above table.',
                   'state_at_shot_end': 'Cup rests on table.', 'opening_characters': []}
        with patch.object(director, 'call_agent', return_value={'changes':changes}) as model:
            output = director.refresh_edited_shot_direction([revised, sibling], 1,
                {'characters': []}, {'scenes':[{'scene_number':1,'description':'Cup moves'}]})
        self.assertEqual(output[1], sibling)
        self.assertEqual(output[0]['description'], revised['description'])
        self.assertEqual(output[0]['shot_direction']['action_beats'], refreshed['action_beats'])
        self.assertEqual(output[0]['state_at_shot_end'], 'Cup rests on table.')
        self.assertNotIn('http', model.call_args.args[1])

    def test_simple_edit_never_saves_an_incomplete_direction(self):
        shot = directed()['shots'][0]
        shot.pop('direction_source', None)
        with patch.object(director, 'call_agent', return_value={
                'changes':{'state_at_shot_start':'Still at table.'}}) as model:
            with self.assertRaisesRegex(ValueError, 'previous version is unchanged'):
                director.refresh_edited_shot_direction([shot], 1,
                    {'characters': []}, {'scenes': []})
        self.assertEqual(model.call_count, 2)

    def test_execution_contract_is_all_or_none_and_legacy_remains_usable(self):
        shot = directed()['shots'][0]
        self.assertEqual(ad_direction.problems(shot), [])
        shot['shot_direction']['blocking'] = 'Hand enters from screen right; cup stays centered.'
        self.assertTrue(ad_direction.problems(shot))
        shot['shot_direction'].update(action_beats=['Hand approaches the handle.', 'Hand lifts the cup and holds.'],
                                     critical_outcome='Cup visibly clears the table.')
        self.assertEqual(ad_direction.problems(shot), [])
        shot['shot_direction']['action_beats'] = ['']
        self.assertTrue(ad_direction.problems(shot))

    def test_dialogue_delivery_has_one_authoritative_beat(self):
        shot = directed()['shots'][0]
        shot.update(speech_mode='onscreen', has_dialogue=True)
        shot.pop('direction_source', None)
        shot['shot_direction'].update(
            blocking='Speaker remains beside the table.',
            critical_outcome='The listener hears the complete line.',
            action_beats=['Listener accepts the cup.', 'Speaker says the approved line.', 'Listener reacts.'],
            dialogue_beat_index=2)
        self.assertEqual(ad_direction.problems(shot), [])
        shot['shot_direction']['action_beats'][1] = 'Speaker says the approved line only after the listener drinks.'
        self.assertIn('actions required before speech', ' '.join(ad_direction.problems(shot)))
        shot['shot_direction']['action_beats'] = ['Speaker whispers early.', 'Speaker says the approved line.', 'Listener reacts.']
        self.assertIn('only the selected dialogue beat', ' '.join(ad_direction.problems(shot)))

    def test_ordered_execution_reaches_final_video_request_not_opening_preview(self):
        from app.services import video_generation_service, director_review
        result = directed()
        shot = result['shots'][0]
        result['continuity']['characters'] = [{'name': 'Meera', 'description': 'A woman holding a cup'}]
        shot.update(speech_mode='onscreen', speaker_label='Meera', speaker_name='Meera',
                    characters_in_shot=['Meera'], opening_characters=['Meera'],
                    dialogue_audio_url='https://example.com/audio.wav', dialogue_audio_duration_sec=4.5,
                    still_frame_url='https://example.com/scene.jpg', still_frame_status='ready')
        shot.pop('direction_source', None)
        shot['shot_direction'].update(blocking='The cup stays centered; Meera is screen right.',
            action_beats=['Meera reaches for the handle.', 'She lifts the cup and speaks the approved line, then settles her hand.'],
            dialogue_beat_index=2,
            critical_outcome='The cup visibly clears the table.')
        ad_direction.accept_shots([shot])
        result.update(director.validate_and_correct([shot], [], 5))
        result['assembly'] = director_review.timeline(result['shots'])
        result.update(video_model='seedance_mini_evolink', language='Hindi', quality='720p')
        output=shot_prompt_compiler.compile_shot_prompts(result, emit=lambda *a:None,
            call_agent=lambda *a,**k: self.fail('No extra model pass permitted'))[0]
        prompt=video_generation_service.translate(result, output)['request']['prompt']
        for value in [shot['shot_direction']['blocking'], shot['shot_direction']['critical_outcome'], *shot['shot_direction']['action_beats'], shot['dialogue_text']]:
            self.assertIn(value, prompt)
        self.assertLess(prompt.index('Meera reaches'),prompt.index('She lifts'))
        still=preview_plan.preview_visual(preview_plan.preview_input(result,shot))
        self.assertNotIn('She lifts the cup, then settles',still)

    def test_coverage_cannot_approve_omitted_or_unsubstantiated_scene(self):
        story = {'scenes':[{'scene_number':1,'heading':'Two attempts'}]}
        shots = [{'shot_number':1,'scene_number':1}]
        with self.assertRaisesRegex(ValueError, 'omitted scene coverage'):
            ad_direction.check_coverage({'approved':True,'issues':[]},shots,story)
        row = {'scene_number':1,'shot_numbers':[1],'covered':False,'evidence':'Only first attempt shown'}
        verdict = ad_direction.check_coverage({'approved':True,'issues':[],'scene_coverage':[row]},shots,story)
        self.assertFalse(verdict['approved'])
        self.assertEqual(verdict['issues'][0]['code'],'story_coverage')
        for numbers in ([], [99], [1,1]):
            with self.assertRaisesRegex(ValueError, 'invalid coverage'):
                ad_direction.check_coverage({'approved':True,'scene_coverage':[{**row,'covered':True,'shot_numbers':numbers}]},shots,story)

    def test_coverage_evidence_never_clears_other_semantic_issues(self):
        story = {'scenes':[{'scene_number':1,'description':'Two complete actions'}]}
        shots = [{'shot_number':1,'scene_number':1}]
        issue = {'shot_number':1,'problem':'Split utterance','fix_instruction':'Keep complete speech'}
        verdict = ad_direction.check_coverage({'approved':True,'issues':[issue], 'scene_coverage':[
            {'scene_number':1,'shot_numbers':[1],'covered':True,'evidence':'Both actions shown'}]}, shots,story)
        self.assertFalse(verdict['approved'])
        self.assertIn(issue,verdict['issues'])

    def test_invalid_camera_repairs_only_camera_despite_dialogue_warning(self):
        from app.services.camera_direction import check_plan
        shots = directed()['shots']
        shots[0]['camera_direction'] = dict(movement='push',direction='in',speed='quick',stabilization='handheld')
        qa = check_plan({'approved':True,'issues':[]},shots)
        permissions = patch_permissions(shots,qa['issues'])
        self.assertEqual(permissions,{1:['camera_direction']})
        with self.assertRaisesRegex(ValueError,'unauthorized field'):
            apply_patch_response(shots,{'patches':[{'shot_number':1,'changes':{'dialogue_text':'Changed'}}]},permissions)

    def test_image_is_opening_not_performance_or_ending(self):
        result = directed()
        facts = preview_plan.preview_input(result, result['shots'][0])
        visual = preview_plan.preview_visual(facts)
        self.assertIn('Blue cup on table', visual)
        for forbidden in ('Blue cup held above table', 'The hand lifts', 'Quiet piano'):
            self.assertNotIn(forbidden, visual)
        self.assertIn('Preserve the real markings', visual)

    def test_video_receives_full_directed_action_without_music(self):
        result = directed()
        payload = shot_prompt_compiler.compiler_input(result, emit=lambda *a: None, camera_contract=True)
        self.assertEqual(payload['shots'][0]['shot_direction'], result['shots'][0]['shot_direction'])
        self.assertEqual(payload['shots'][0]['description'], 'The hand lifts the blue cup')
        self.assertIn('Blue cup held above table', json.dumps(payload))
        self.assertNotIn('Quiet piano', json.dumps(payload))

    def test_edit_cannot_use_stale_direction_in_either_adapter(self):
        result = directed()
        result['shots'][0]['description'] = 'The hand puts the cup down'
        with self.assertRaisesRegex(ValueError, 'direction needs review'):
            preview_plan.preview_input(result, result['shots'][0])
        with self.assertRaisesRegex(ValueError, 'direction needs review'):
            shot_prompt_compiler.compiler_input(result, emit=lambda *a: None)


    def test_missing_direction_is_not_silently_accepted(self):
        result = directed()
        result['shots'][0]['shot_direction'] = {}
        qa = ad_direction.check_plan({'approved': True, 'issues': []}, result['shots'])
        self.assertFalse(qa['approved'])
        permissions = patch_permissions(result['shots'], qa['issues'])
        with self.assertRaises(ValueError):
            apply_patch_response(result['shots'], {'patches': [{'shot_number': 1,
                'changes': {'dialogue_text': 'Different'}}]}, permissions)


    def test_legacy_adapter_remains_available(self):
        result = source()
        self.assertIsNone(ad_direction.visual_direction(result))
        self.assertIn('Blue cup', preview_plan.preview_visual(preview_plan.preview_input(result, result['shots'][0])))

    def test_direction_changes_invalidate_preview_inputs(self):
        result = directed()
        before = preview_plan.preview_input(result, result['shots'][0])
        result['ad_direction']['visual_approach'] = 'Approved warmer light treatment'
        self.assertNotEqual(before, preview_plan.preview_input(result, result['shots'][0]))

    def test_all_literal_framing_duplicates_collected_in_one_pass(self):
        shots = [copy.deepcopy(directed()['shots'][0]) for _ in range(4)]
        for n, shot in enumerate(shots, 1):
            shot['shot_number'] = n
        qa = ad_direction.check_plan({'approved': True, 'issues': []}, shots)
        self.assertEqual([i['shot_number'] for i in qa['issues']], [2, 3, 4])
        self.assertEqual(patch_permissions(shots, qa['issues']), {2:['camera_angle'], 3:['camera_angle'], 4:['camera_angle']})

    def test_minimum_duration_repair_cannot_rewrite_story(self):
        from app.services.planning_contract import check_mechanics
        result = directed()
        shot = result['shots'][0]
        shot['duration_sec'] = 3
        qa = check_mechanics({'approved':True,'issues':[]}, result['shots'], [], 4)
        permissions = patch_permissions(result['shots'], qa['issues'])
        self.assertEqual(permissions, {1:['duration_sec']})
        updated = apply_patch_response(result['shots'], {'patches':[{'shot_number':1,'changes':{'duration_sec':4}}]}, permissions)
        self.assertEqual(updated[0]['description'], shot['description'])
        self.assertEqual(updated[0]['shot_direction'], shot['shot_direction'])


    def test_later_arrival_not_in_opening_image_or_entity_references(self):
        from app.services.still_frame_service import match_entities
        result = directed()
        result['continuity']['characters'] = [{'name':'Carpenter','description':'A carpenter'},
                                              {'name':'Spirit','description':'Translucent spirit'}]
        shot = result['shots'][0]
        shot.update(characters_in_shot=['Carpenter','Spirit'],opening_characters=['Carpenter'],
                    description='Carpenter falls, then Spirit rises',state_at_shot_start='Carpenter on tipping stool',
                    state_at_shot_end='Spirit floats above the fallen Carpenter')
        shot.pop('direction_source')
        ad_direction.accept_shots([shot])
        facts = preview_plan.preview_input(result,shot)
        visual = preview_plan.preview_visual(facts)
        self.assertNotIn('Spirit',visual)
        self.assertNotIn('characters:spirit',match_entities(result,shot,visual))
        payload=shot_prompt_compiler.compiler_input(result,emit=lambda *a:None,camera_contract=True)
        self.assertIn('Spirit',payload['shots'][0]['characters_in_shot'])
        self.assertEqual(payload['shots'][0]['opening_characters'],['Carpenter'])

    def test_absent_character_style_never_leaks_into_preview_or_video(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        result['continuity']['characters'] = [{'name': 'Yamaraj', 'description': 'A deity'}]
        result['continuity']['visual_style'] = {
            'rendering': 'Natural live action, lifelike skin texture, crisp fabric product detail',
            'lighting_motif': 'Warm workshop light, golden glow around Yamaraj',
        }
        result['ad_direction']['visual_approach'] = 'Warm workshop light, Yamaraj behind the bucket'
        shot = result['shots'][0]
        opening = preview_plan.preview_input(result, shot)
        video = visual_instruction(result, shot, 'the accepted preview', speaking=False)
        self.assertIn('Warm workshop light', opening['visual_style']['lighting_motif'])
        self.assertIn('Warm workshop light', video)
        self.assertIn('crisp fabric product detail', video)
        for forbidden in ('Yamaraj', 'skin texture'):
            self.assertNotIn(forbidden, preview_plan.generation_prompt(preview_plan.visual_contract(opening)))
            self.assertNotIn(forbidden, video)

    def test_ordered_beats_are_only_action_source_for_silent_video(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        shot = result['shots'][0]
        shot['description'] = 'The cup has already been lifted.'
        shot['shot_direction']['action_beats'] = ['Hand reaches for cup.', 'Hand lifts cup.']
        prompt = visual_instruction(result, shot, 'the accepted preview', speaking=False)
        self.assertNotIn('already been lifted', prompt)
        self.assertLess(prompt.index('Hand reaches for cup.'), prompt.index('Hand lifts cup.'))

    def test_faceless_product_hero_does_not_add_unscheduled_effect_from_performance(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        shot = result['shots'][0]
        shot['characters_in_shot'] = []
        shot['shot_direction']['action_beats'] = ['Hold cut loaf and single slice on board.',
                                                   'Sunlight reveals crust and crumb.']
        shot['shot_direction']['performance'] = 'Steam wisps rise above the bread.'
        shot['shot_direction']['forbidden_geometry'] = ['No artificial steam jets.']
        prompt = visual_instruction(result, shot, 'the accepted preview', speaking=False)
        self.assertIn('Hold cut loaf and single slice on board.', prompt)
        self.assertNotIn('Performance: Steam wisps', prompt)
        self.assertIn('No artificial steam jets.', prompt)

    def test_forbidden_rules_are_not_negated_twice_in_video_prompt(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        shot = result['shots'][0]
        shot['shot_direction']['forbidden_geometry'] = ['The cup must not float.']
        prompt = visual_instruction(result, shot, 'the accepted preview', speaking=False)
        self.assertIn('Hard staging rules: The cup must not float.', prompt)
        self.assertNotIn('Never show: The cup must not float.', prompt)
        self.assertIn('no dialogue, muttering, speech-like lip articulation', prompt)

    def test_character_entering_later_is_not_in_opening_look(self):
        result = directed()
        result['continuity']['characters'] = [{'name': 'Yamaraj', 'description': 'A deity'}]
        result['continuity']['visual_style'] = {'lighting_motif':
            'Warm workshop light, golden glow around Yamaraj'}
        shot = result['shots'][0]
        shot['characters_in_shot'] = ['Yamaraj']
        shot.pop('direction_source', None)
        opening = preview_plan.preview_input(result, shot)
        self.assertNotIn('Yamaraj', str(opening['visual_style']))
        self.assertIn('Yamaraj', str(ad_direction.shot_visual_style(result, shot)))

    def test_explicit_hero_light_excludes_global_absent_prop_motif(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        shot = result['shots'][0]
        shot.update(description='A Fevicol bucket stands on the bench.',
                    state_at_shot_start='The Fevicol bucket rests on the bench; no people present.',
                    lighting='Warm golden key light on the bucket.', characters_in_shot=[],
                    opening_characters=[])
        result['continuity']['visual_style'] = {
            'rendering': 'Photorealistic live-action',
            'palette': 'Warm wood browns, golden accent on lasso and Fevicol bucket',
            'lighting_motif': 'Cool fog when the spirit appears',
        }
        result['entity_references'] = {'props:lasso': {'url': 'https://example.com/lasso.jpg'}}
        prompt = visual_instruction(result, shot, 'the accepted preview', speaking=False)
        self.assertIn('Warm golden key light', prompt)
        self.assertNotIn('lasso', prompt.casefold())
        self.assertNotIn('Cool fog', prompt)

    def test_product_shot_omits_unreferenced_story_prop_in_global_palette(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        result['continuity']['visual_style'] = {
            'palette': 'Neutral true-to-life colors, with golden accent only on lasso and Fevicol bucket highlights'}
        shot = result['shots'][0]
        shot.update(characters_in_shot=[], description='One Fevicol bucket on a table.',
                    state_at_shot_start='One Fevicol bucket on a table; no human figures present.',
                    state_at_shot_end='The same Fevicol bucket remains; no people present.')
        prompt = visual_instruction(result, shot, 'the supplied image')
        self.assertNotIn('lasso', prompt.casefold())
        self.assertIn('No person, silhouette', prompt)
        self.assertIn('new prop', prompt)

    def test_new_style_projection_keeps_accepted_preview_when_only_absent_prop_was_removed(self):
        from app.services.still_frame_service import invalidate_changed_stills, shot_fingerprint
        result = directed()
        result['continuity']['characters'] = [{'name': 'Yamaraj'}]
        result['continuity']['visual_style'] = {
            'rendering': 'Natural, Yamaraj blue skin',
            'palette': 'Neutral true-to-life colors, with golden accent only on lasso and Fevicol bucket highlights'}
        shot = result['shots'][0]
        shot.update(characters_in_shot=[], description='One Fevicol bucket on a table.',
                    state_at_shot_start='One Fevicol bucket on a table; no human figures present.',
                    state_at_shot_end='The same Fevicol bucket remains; no people present.')
        shot['direction_source'] = ad_direction.source_key(shot)
        saved = preview_plan.preview_input(result, shot)
        saved['visual_style']['palette'] = result['continuity']['visual_style']['palette']
        shot.update(preview_input=saved, still_frame_key='accepted.jpg')
        shot['still_frame_source_hash'] = shot_fingerprint(shot)
        invalidate_changed_stills(result)
        self.assertEqual(shot['still_frame_key'], 'accepted.jpg')

    def test_spirit_and_product_only_shots_keep_visual_identity_boundaries(self):
        from app.services.dialogue_window import visual_instruction
        result = directed()
        spirit = result['shots'][0]
        spirit.update(characters_in_shot=['Carpenter'],
                      description='The spirit of the Carpenter rises from his prone body.',
                      state_at_shot_start='The Carpenter lies supine on the floor.',
                      state_at_shot_end='The Carpenter spirit hovers over the body.')
        prompt = visual_instruction(result, spirit, 'Image 1', speaking=False)
        self.assertIn('translucent visual double of Carpenter', prompt)
        self.assertIn('Never render a generic faceless', prompt)

        hero = result['shots'][0]
        hero.update(characters_in_shot=[], description='The product sits on a workbench.',
                    state_at_shot_start='The product sits on a bench; no human figures present.',
                    state_at_shot_end='The product remains on a bench; no people present.')
        prompt = visual_instruction(result, hero, 'the supplied image')
        self.assertIn('No person, silhouette, reflection, hand, new prop', prompt)




if __name__ == '__main__':
    unittest.main()
