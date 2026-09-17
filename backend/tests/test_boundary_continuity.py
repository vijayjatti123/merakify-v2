import copy
import unittest
from unittest.mock import Mock, patch

from app.services import boundary_continuity as boundary
from app.services import shot_prompt_compiler as compiler
from app.agents import director
from tests.test_shot_prompt_compiler import source


def plan():
    result = source()
    result['shots'][0].update(description='Stool cracks, carpenter falls backward.',
        state_at_shot_end='Stool broken, carpenter mid-fall to floor', still_frame_url='saved-left')
    result['shots'].append({**result['shots'][0], 'shot_number': 2,
        'description': 'Spirit rises calm; Yamaraj arrives in fog.',
        'state_at_shot_start': "Carpenter's spirit rising, fog thick, Yamaraj unseen",
        'still_frame_url': 'saved-right'})
    result['assembly']['transitions'] = [{'between': '1-2', 'type': 'match cut',
                                         'reason': 'fall transitions into spirit rising'}]
    return result


def decision(relation='narrative_transition', approved=True):
    return {'boundaries': [{'between': '1-2', 'approved': approved, 'relation': relation,
        'reason': 'Source transforms the falling body into its rising spirit.',
        'shared_physical_state': 'The same grounded instant.' if relation == 'same_instant' else None}]}


class BoundaryTests(unittest.TestCase):
    def test_saved_narrative_match_passes_without_changing_shots_or_cut(self):
        result = plan()
        original = copy.deepcopy(result)
        with self.assertRaisesRegex(ValueError, 'Physical state boundary 1-2'):
            compiler.compiler_input(result, emit=Mock())
        call = Mock(return_value=decision())
        boundary.prepare_boundaries(result, emit=Mock(), call_agent=call)
        payload = compiler.compiler_input(result, emit=Mock())
        self.assertEqual(payload['boundaries'][0]['temporal_relation'], 'narrative_transition')
        self.assertNotIn('shared_physical_state', payload['boundaries'][0])
        self.assertEqual(result['shots'], original['shots'])
        self.assertEqual(result['assembly'], original['assembly'])
        boundary.prepare_boundaries(result, emit=Mock(), call_agent=call)
        self.assertEqual(call.call_count, 1)

    def test_changed_plan_or_brief_invalidates_review(self):
        result = plan()
        boundary.prepare_boundaries(result, emit=Mock(), call_agent=Mock(return_value=decision()))
        self.assertEqual(boundary.reviewed_boundaries(result, 'different brief'), {})
        result['shots'][1]['state_at_shot_start'] = 'Stool intact again without explanation'
        with self.assertRaisesRegex(ValueError, 'Physical state boundary'):
            compiler.compiler_input(result, emit=Mock())

    def test_progression_and_equivalent_snapshots_have_distinct_contracts(self):
        for relation in ('action_progression', 'same_instant'):
            result = plan()
            boundary.prepare_boundaries(result, emit=Mock(), call_agent=Mock(return_value=decision(relation)))
            item = compiler.compiler_input(result, emit=Mock())['boundaries'][0]
            self.assertEqual('shared_physical_state' in item, relation == 'same_instant')

    def test_real_conflict_still_blocks_before_any_media_work(self):
        result = plan()
        original = copy.deepcopy(result)
        call = Mock(return_value=decision('conflict', False))
        with patch.object(director, 'call_agent', call), \
             patch.object(director, 'generate_still_frames') as images, \
             patch.object(director, 'compile_shot_prompts') as compile_call:
            with self.assertRaisesRegex(ValueError, 'Shot transitions need review'):
                director._prepare_media_parallel(Mock(), 'audit', result, brief='', emit=Mock())
        images.assert_not_called()
        compile_call.assert_not_called()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result, original)

    def test_identical_physical_states_need_no_provider_review(self):
        result = plan()
        result['shots'][1]['state_at_shot_start'] = result['shots'][0]['state_at_shot_end']
        call = Mock()
        boundary.prepare_boundaries(result, emit=Mock(), call_agent=call)
        call.assert_not_called()
        self.assertIn('shared_physical_state', compiler.compiler_input(result, emit=Mock())['boundaries'][0])

    def test_assembler_label_alone_cannot_bypass_a_conflict(self):
        result = plan()
        result['assembly']['transitions'][0]['temporal_relation'] = 'narrative_transition'
        with self.assertRaisesRegex(ValueError, 'Physical state boundary'):
            compiler.compiler_input(result, emit=Mock())

    def test_bad_or_partial_review_gets_one_correction_then_fails_closed(self):
        result = plan()
        call = Mock(return_value={'boundaries': []})
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            boundary.prepare_boundaries(result, emit=Mock(), call_agent=call)
        self.assertEqual(call.call_count, 2)
        self.assertNotIn('boundary_continuity_review', result)

    def test_failure_does_not_clear_accepted_previews(self):
        result = plan()
        call = Mock(side_effect=TimeoutError('injected timeout'))
        with self.assertRaises(TimeoutError):
            boundary.prepare_boundaries(result, emit=Mock(), call_agent=call)
        self.assertEqual([s['still_frame_url'] for s in result['shots']], ['saved-left', 'saved-right'])
