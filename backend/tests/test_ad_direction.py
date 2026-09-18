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

    def test_edited_action_repaired_in_existing_loop_and_dialogue_preserved(self):
        result = directed()
        shot = result['shots'][0]
        shot['description'] = 'The hand puts the cup down'
        calls = []
        def provider(system, content, **kwargs):
            calls.append(system)
            if system == prompts.CINEMATOGRAPHY_PATCH:
                self.assertIn('ad_direction', content)
                return {'patches': [{'shot_number': 1, 'changes': dict(
                    shot_direction={**shot['shot_direction'], 'performance': 'The hand gently lowers the cup'},
                    opening_characters=[],
                    state_at_shot_start='Blue cup in hand above table.',
                    state_at_shot_end='Blue cup rests on table.')} ]}
            return {'approved': True, 'issues': []}
        with patch.object(director, 'call_agent', side_effect=provider):
            output = director.validate_and_correct(result['shots'], [], 5,
                ad_direction_plan=result['ad_direction'], approved_story=result['script'])
        self.assertEqual(calls, [prompts.QA_AGENT, prompts.CINEMATOGRAPHY_PATCH, prompts.QA_AGENT])
        self.assertEqual(output['shots'][0]['dialogue_text'], 'Look here.')
        self.assertEqual(output['shots'][0]['description'], 'The hand puts the cup down')
        self.assertFalse(ad_direction.problems(output['shots'][0]))

    def test_missing_direction_is_not_silently_accepted(self):
        result = directed()
        result['shots'][0]['shot_direction'] = {}
        qa = ad_direction.check_plan({'approved': True, 'issues': []}, result['shots'])
        self.assertFalse(qa['approved'])
        permissions = patch_permissions(result['shots'], qa['issues'])
        with self.assertRaises(ValueError):
            apply_patch_response(result['shots'], {'patches': [{'shot_number': 1,
                'changes': {'dialogue_text': 'Different'}}]}, permissions)

    def test_directed_plan_semantic_rejection_remains_blocking(self):
        result = directed()
        issue = dict(shot_number=1, problem='Impossible action', fix_instruction='Repair action')
        def provider(system, content, **kwargs):
            if system == prompts.CINEMATOGRAPHY_FIX:
                return {'shots': copy.deepcopy(result['shots'])}
            return {'approved': False, 'issues': [issue]}
        with patch.object(director, 'call_agent', side_effect=provider):
            with self.assertRaisesRegex(ValueError, 'unresolved checks'):
                director.validate_and_correct(result['shots'], [], 5)

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

    def test_added_shot_cannot_downgrade_to_legacy_contract(self):
        result = directed()
        issue = dict(shot_number=1,problem='Missing story beat',fix_instruction='Restore the required action')
        def provider(system, content, **kwargs):
            if system == prompts.CINEMATOGRAPHY_FIX:
                return {'shots':[copy.deepcopy(result['shots'][0]), {**copy.deepcopy(source()['shots'][0]),
                    'shot_number':2,'duration_sec':5,'speech_mode':'none'}]}
            return {'approved':True,'issues':[]} if '"shot_number": 2' in content else {'approved':False,'issues':[issue]}
        with patch.object(director,'call_agent',side_effect=provider):
            with self.assertRaisesRegex(ValueError,'unresolved checks'):
                director.validate_and_correct(result['shots'],[],5,ad_direction_plan=result['ad_direction'])

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

    def test_semantic_qa_retains_direction_but_omits_media_payload(self):
        result=directed()
        result['shots'][0].update(still_frame_url='PRIVATE_IMAGE', compiled_prompt='OLD_GENERATED_PROSE',
                                 dialogue_audio_url='PRIVATE_AUDIO')
        with patch.object(director,'call_agent',return_value={'approved':True,'issues':[]}) as call:
            director.validate_and_correct(result['shots'],[],5,
                ad_direction_plan=result['ad_direction'],approved_story=result['script'])
        content=call.call_args.args[1]
        self.assertIn('An unhurried hand lifts the cup',content)
        self.assertIn('approved_story',content)
        for excluded in ('PRIVATE_IMAGE','PRIVATE_AUDIO','OLD_GENERATED_PROSE'):
            self.assertNotIn(excluded,content)
        self.assertGreater(call.call_args.kwargs['max_tokens'],3072)
        self.assertEqual(call.call_count,1)
        from app.agents.execution import stage_budget
        self.assertEqual(stage_budget('qa_agent'),90)

    def test_complex_review_gets_recovery_allowance_without_a_truncation_rerun(self):
        result = directed()
        shots = [copy.deepcopy(result['shots'][0]) for _ in range(8)]
        for number, shot in enumerate(shots, 1):
            shot.update(shot_number=number, camera_angle=f'Viewpoint {number}')
        with patch.object(director, 'call_agent', return_value={'approved':True,'issues':[]}) as call:
            director.validate_and_correct(shots, [], 40,
                ad_direction_plan=result['ad_direction'], approved_story=result['script'])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs['max_tokens'], 8192)
        self.assertNotIn('truncation_retry_tokens', call.call_args.kwargs)
        # A legacy plan must not inherit the new directed-plan budget.
        legacy = copy.deepcopy(result['shots'])
        for shot in legacy:
            shot.pop('direction_version')
        with patch.object(director, 'call_agent', return_value={'approved':True,'issues':[]}) as call:
            director.validate_and_correct(legacy, [], 5)
        self.assertEqual(call.call_args.kwargs, {'max_tokens':3072})


if __name__ == '__main__':
    unittest.main()
