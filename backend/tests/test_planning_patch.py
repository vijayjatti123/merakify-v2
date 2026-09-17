import copy
import unittest
from unittest.mock import patch

from app.services.planning_patch import patch_permissions, apply_patch_response
from app.agents import director, prompts


class PlanningPatchTests(unittest.TestCase):
    def setUp(self):
        self.shots = [dict(shot_number=n, scene_number=1, duration_sec=5,
            description='Person holds a cup', camera_angle='eye-level close-up',
            camera_movement='static', has_dialogue=True, speech_mode='onscreen',
            dialogue_text=f'Complete line {n}.', characters_in_shot=['Meera']) for n in (1, 2)]
        self.issue = dict(shot_number=2, problem='Identical scale/angle', fix_instruction='Vary camera angle')

    def test_only_authorized_field_changes(self):
        original = copy.deepcopy(self.shots)
        permissions = patch_permissions(self.shots, [self.issue])
        response = {'patches': [{'shot_number': 2, 'changes': {'camera_angle': 'high-angle medium'}}]}
        result = apply_patch_response(self.shots, response, permissions)
        self.assertEqual(result[0], original[0])
        expected = {**original[1], 'camera_angle': 'high-angle medium'}
        self.assertEqual(result[1], expected)
        self.assertEqual(self.shots, original)

    def test_rejects_unrelated_dialogue_and_sibling_changes(self):
        permissions = patch_permissions(self.shots, [self.issue])
        for number, changes in [(1, {'camera_angle': 'wide'}), (2, {'dialogue_text': 'Changed'}),
                                (2, {'camera_angle': ''})]:
            with self.assertRaises(ValueError):
                apply_patch_response(self.shots, {'patches': [{'shot_number': number, 'changes': changes}]}, permissions)

    def test_structural_and_unknown_findings_use_existing_path(self):
        for problem in ('Split utterance across shots', 'Boundary state mismatch', '180-degree axis crossing', 'Something unclear'):
            self.assertIsNone(patch_permissions(self.shots, [{'shot_number': 2, 'problem': problem}]))

    def test_actual_director_patch_and_full_semantic_recheck(self):
        calls = []
        def provider(system, content, **kwargs):
            calls.append(system)
            if system == prompts.CINEMATOGRAPHY_PATCH:
                return {'patches': [{'shot_number': 2, 'changes': {'camera_angle': 'high-angle medium'}}]}
            return {'approved': len(calls) > 1, 'issues': [] if len(calls) > 1 else [self.issue]}
        for source in (None, 'Meera: Complete line 1. Meera: Complete line 2.'):
            calls.clear()
            with patch.object(director, 'call_agent', side_effect=provider):
                result = director.validate_and_correct(copy.deepcopy(self.shots), [{'name': 'Meera'}], 10,
                                                      source_script_text=source)
            self.assertEqual(calls, [prompts.QA_AGENT, prompts.CINEMATOGRAPHY_PATCH, prompts.QA_AGENT])
            self.assertTrue(result['qa']['approved'])
            self.assertNotIn('dialogue_loss_warnings', result['qa'])
            self.assertEqual([s['dialogue_text'] for s in result['shots']], [s['dialogue_text'] for s in self.shots])

    def test_no_correction_when_qa_passes(self):
        with patch.object(director, 'call_agent', return_value={'approved': True, 'issues': []}) as call:
            director.validate_and_correct(copy.deepcopy(self.shots), [{'name': 'Meera'}], 10)
        self.assertEqual(call.call_count, 1)
