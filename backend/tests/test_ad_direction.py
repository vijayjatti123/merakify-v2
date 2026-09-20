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




if __name__ == '__main__':
    unittest.main()
