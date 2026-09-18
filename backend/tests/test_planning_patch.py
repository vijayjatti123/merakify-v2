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

    def test_user_review_preserves_story_without_requesting_a_patch(self):
        for source in (None, 'Meera: Complete line 1. Meera: Complete line 2.'):
            with patch.object(director, 'call_agent', side_effect=AssertionError('No semantic QA or patch')) as call:
                result = director.validate_and_correct(copy.deepcopy(self.shots), [{'name':'Meera'}], 10, source_script_text=source)
            call.assert_not_called()
            self.assertEqual([s['dialogue_text'] for s in result['shots']], [s['dialogue_text'] for s in self.shots])
            self.assertEqual([s['camera_angle'] for s in result['shots']], [s['camera_angle'] for s in self.shots])

    def test_invalid_camera_returns_technical_issue_for_user_correction(self):
        shots = copy.deepcopy(self.shots)
        shots[1]['camera_direction'] = dict(movement='zoom', direction='in', speed='slow', stabilization='locked')
        result = director.validate_and_correct(shots, [{'name':'Meera'}], 10)
        self.assertFalse(result['qa']['approved'])
        self.assertTrue(any(i['code']=='invalid_camera_direction' for i in result['qa']['issues']))
