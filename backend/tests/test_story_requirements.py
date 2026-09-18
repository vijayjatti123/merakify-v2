import copy
import unittest
import json
from unittest.mock import patch
from app.services import story_requirements as req
from app.services.planning_patch import patch_permissions, apply_patch_response, protect_unflagged, apply_insertion_response


class StoryRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.story = {'scenes': [{'scene_number': 1, 'heading': 'Two tries',
            'description': 'He throws once. He throws again.', 'dialogue_or_vo': 'Try again.'}]}
        self.shots = [{'shot_number': 1, 'scene_number': 1, 'description': 'First throw',
            'dialogue_text': 'Try again.'}, {'shot_number': 2, 'scene_number': 1, 'description': 'Second throw'}]
        self.rows = [{'requirement_id': r['id'], 'shot_numbers': [1, 2], 'covered': True,
                      'evidence': 'First throw then second throw'} for r in req.requirements(self.story)]
        self.checks = [{'shot_number': n, 'consistent': True, 'evidence': 'States agree'} for n in (1, 2)]

    def test_quotes_not_summarized_and_each_sentence_tracked(self):
        self.assertEqual([r['quote'] for r in req.requirements(self.story)],
            ['Two tries', 'He throws once.', 'He throws again.', 'Try again.'])

    def test_missing_duplicate_and_wrong_scene_evidence_cannot_pass(self):
        bads = [self.rows[:-1], self.rows[:-1] + [self.rows[0]],
                [{**r, 'shot_numbers': [99]} for r in self.rows]]
        for rows in bads:
            with self.assertRaises(ValueError):
                req.check_review({'approved': True, 'issues': [], 'requirement_coverage': rows}, self.shots, self.story)

    def test_uncovered_requires_linked_actionable_target(self):
        rows = copy.deepcopy(self.rows)
        rows[2].update(covered=False, shot_numbers=[])
        verdict = {'approved': True, 'issues': [], 'requirement_coverage': rows, 'shot_checks': self.checks}
        with self.assertRaises(ValueError):
            req.check_review(verdict, self.shots, self.story)
        verdict['issues'] = [{'shot_number': 2, 'requirement_id': rows[2]['requirement_id']}]
        self.assertFalse(req.check_review(verdict, self.shots, self.story)['approved'])

    def test_narrow_visual_patch_preserves_speech_and_neighbors(self):
        issue = {'shot_number': 2, 'repair_kind': 'visual_fields', 'repair_fields': ['lighting']}
        permissions = patch_permissions(self.shots, [issue])
        result = apply_patch_response(self.shots, {'patches': [{'shot_number': 2,
            'changes': {'lighting': 'Warm side light'}}]}, permissions)
        self.assertEqual(result[0], self.shots[0])
        for forbidden in ('dialogue_text', 'characters_in_shot', 'scene_number'):
            with self.assertRaises(ValueError):
                patch_permissions(self.shots, [{**issue, 'repair_fields': [forbidden]}])

    def test_structural_response_cannot_rewrite_neighbors(self):
        changed = copy.deepcopy(self.shots)
        changed[0]['description'] = 'Unrequested change'
        with self.assertRaises(ValueError):
            protect_unflagged(self.shots, changed, [{'shot_number': 2}])
        protect_unflagged(self.shots, self.shots, [{'shot_number': 2}])

    def test_real_loop_sends_preflight_and_rechecks_scoped_repair(self):
        from app.agents import director, prompts
        from test_ad_direction import directed
        plan = directed()
        shots = plan['shots']
        story = {'scenes': [{'scene_number': shots[0]['scene_number'], 'heading': 'Cup',
                            'description': 'A hand presents the blue cup.'}],
                 'production_context': {'products': [{'name': 'Blue cup'}]}}
        requirements = req.requirements(story)
        number = shots[0]['shot_number']
        calls = []
        def provider(system, content, **kwargs):
            calls.append(system)
            if system == prompts.CINEMATOGRAPHY_PATCH:
                return {'patches': [{'shot_number': number, 'changes': {'lighting': 'Soft side light'}}]}
            payload = json.loads(content)
            self.assertEqual(payload['source_context'], story['production_context'])
            self.assertIn('mechanical_findings', payload)
            return {'approved': len(calls) > 1,
                'shot_checks': [{'shot_number': number, 'consistent': True, 'evidence': 'States agree'}],
                'scene_coverage': [{'scene_number': shots[0]['scene_number'], 'shot_numbers': [number],
                                    'covered': True, 'evidence': 'Cup presented'}],
                'requirement_coverage': [{'requirement_id': r['id'], 'shot_numbers': [number],
                    'covered': True, 'evidence': 'Cup presented'} for r in requirements],
                'issues': [] if len(calls) > 1 else [{'shot_number': number,
                    'problem': 'Lighting contradicts source', 'fix_instruction': 'Use soft side light',
                    'repair_kind': 'visual_fields', 'repair_fields': ['lighting'], 'requirement_id': ''}]}
        with patch.object(director, 'call_agent', side_effect=provider):
            result = director.validate_and_correct(shots, [], 5,
                ad_direction_plan=plan['ad_direction'], approved_story=story)
        self.assertTrue(result['qa']['approved'])
        self.assertEqual(calls, [prompts.QA_AGENT, prompts.CINEMATOGRAPHY_PATCH, prompts.QA_AGENT])
        self.assertEqual(result['shots'][0]['dialogue_text'], shots[0]['dialogue_text'])

    def test_missing_execution_check_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, 'execution checks'):
            req.check_review({'approved': True, 'issues': [], 'requirement_coverage': self.rows}, self.shots, self.story)

    def test_incomplete_evidence_is_not_checkpointed_for_retry(self):
        from app.agents import execution, prompts
        from app.services import job_service
        review = {'ad_direction': {'takeaway': 'test'}, 'approved_story': self.story,
                  'requirements': req.requirements(self.story), 'shots': self.shots}
        invalid = {'approved': True, 'issues': [], 'scene_coverage': [
            {'scene_number': 1, 'shot_numbers': [1, 2], 'covered': True, 'evidence': 'Both attempts'}]}
        with patch.object(job_service, 'get_agent_checkpoint', return_value=None), \
             patch.object(job_service, 'save_agent_checkpoint') as save:
            with self.assertRaisesRegex(ValueError, 'required evidence'):
                execution.execute(lambda *a, **kw: invalid, prompts.QA_AGENT, json.dumps(review), {},
                                  (object(), 'isolated', lambda *a: None, 0))
            save.assert_not_called()

    def test_insert_preserves_existing_content_and_speech_correspondence(self):
        shot = dict(scene_number=1, camera_angle='wide', lens='35mm', lighting='warm',
            composition_note='Balanced', description='He tries again', duration_sec=5,
            state_at_shot_start='Rope slack', state_at_shot_end='Rope slack again',
            camera_direction=dict(movement='hold', direction='none', speed='none', stabilization='locked'),
            characters_in_shot=[], opening_characters=[], has_dialogue=False, dialogue_text='', speech_mode='none',
            shot_direction=dict(purpose='Second attempt', performance='Pull then release',
                                product_props='Same rope', edit_intent='Cut to reaction'))
        response = {'patches': [], 'insertions': [{'after_shot_number': 1, 'shot': shot}]}
        result, mapping = apply_insertion_response(self.shots, response, {}, [1])
        self.assertEqual(mapping, {1: 1, 2: 3})
        self.assertEqual(result[0], self.shots[0])
        self.assertEqual(result[2], {**self.shots[1], 'shot_number': 3})
        self.assertEqual(result[0]['dialogue_text'], 'Try again.')
        for changes in ({'dialogue_text': 'Invented', 'has_dialogue': True}, {'scene_number': 2}):
            bad = copy.deepcopy(response)
            bad['insertions'][0]['shot'].update(changes)
            with self.assertRaises(ValueError):
                apply_insertion_response(self.shots, bad, {}, [1])

    def test_insert_loop_keeps_protected_speech_after_ordinal_change(self):
        from app.agents import director, prompts
        from app.agents.output_contracts import contracts
        from app.services import ad_direction
        from test_ad_direction import directed
        plan = directed()
        first = plan['shots'][0]
        second = {**copy.deepcopy(first), 'shot_number': 2, 'dialogue_text': 'It is blue.',
                  'camera_angle': 'high-angle wide'}
        second.pop('direction_source', None)
        shots = [first, second]
        ad_direction.accept_shots(shots)
        story = {'scenes': [{'scene_number': 1, 'heading': 'Two actions'}]}
        new_shot = {k: copy.deepcopy(first.get(k)) for k in
                    contracts()['director-v1']['properties']['shots']['items']['properties'] if k != 'shot_number'}
        new_shot.update(scene_number=1, has_dialogue=False, dialogue_text='', speech_mode='none',
            camera_angle='low-angle wide', camera_direction=dict(movement='hold', direction='none', speed='none', stabilization='locked'))
        for field in ('lens', 'lighting', 'composition_note'):
            new_shot[field] = new_shot[field] or 'Natural'
        calls = []
        def provider(system, content, **kwargs):
            calls.append(system)
            if system == prompts.CINEMATOGRAPHY_PATCH:
                self.assertIn('allowed_insert_after: [1]', content)
                return {'patches': [], 'insertions': [{'after_shot_number': 1, 'shot': new_shot}]}
            reviewed = json.loads(content)['shots']
            approved = len(calls) > 1
            return {'approved': approved,
                'scene_coverage': [{'scene_number': 1, 'shot_numbers': [s['shot_number'] for s in reviewed],
                    'covered': approved, 'evidence': 'Actions checked'}],
                'requirement_coverage': [{'requirement_id': 's1:heading:1', 'shot_numbers': [1],
                    'covered': approved, 'evidence': 'Actions checked'}],
                'shot_checks': [{'shot_number': s['shot_number'], 'consistent': True, 'evidence': 'States agree'} for s in reviewed],
                'issues': [] if approved else [{'shot_number': 1, 'repair_kind': 'insert_after',
                    'requirement_id': 's1:heading:1', 'problem': 'Missing action', 'fix_instruction': 'Add silent reaction'}]}
        notes = []
        with patch.object(director, 'call_agent', side_effect=provider):
            try:
                result = director.validate_and_correct(shots, [], 15,
                    source_script_text=first['dialogue_text'] + ' It is blue.', emit=lambda k,n: notes.append(n),
                    ad_direction_plan=plan['ad_direction'], approved_story=story)
            except ValueError:
                self.fail(str(notes))
        self.assertTrue(result['qa']['approved'])
        self.assertNotIn('dialogue_protection_limitations', result['qa'])
        self.assertEqual(result['shots'][0]['dialogue_text'], first['dialogue_text'])
        self.assertEqual(result['shots'][2]['dialogue_text'], 'It is blue.')
