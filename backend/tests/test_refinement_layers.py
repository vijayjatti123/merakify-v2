import unittest
from unittest.mock import patch

from app.services import clarifier_service as service
from app.agents.output_contracts import contract_for, validate
from app.agents.prompts import refinement_system
import json


class RefinementLayersTests(unittest.TestCase):
    def state(self, script=False):
        return {'raw_brief': 'A cola ad' if not script else 'Tara says: नमस्ते!',
                'known_fields': {'language': 'Hindi'}, 'turns': [],
                'gathered': {'_context': {'ad_type': 'product', 'input_mode': 'script' if script else 'idea'}}}

    def test_missing_fields_repaired_once_without_stacked_retry(self):
        state = self.state()
        with patch.object(service, 'call_agent', side_effect=[{'wrong': 'x'}, {'refined_prompt': 'Proposed direction: a bottle reveal. Avoid: duplicated bottles.'}]) as call:
            result = service.refine_text(state, [])
        self.assertIn('bottle reveal', result)
        self.assertEqual(call.call_count, 2)
        self.assertIn('FORMAT REPAIR', call.call_args.args[0])
        self.assertEqual(json.loads(call.call_args.args[1])['draft_to_repair'], {'wrong': 'x'})
        self.assertNotIn('truncation_retry_tokens', call.call_args.kwargs)
        self.assertLessEqual(call.call_args.kwargs['request_timeout'], call.call_args_list[0].kwargs['request_timeout'])

    def test_second_bad_result_stops_and_diagnostics_exclude_raw_output(self):
        state = self.state()
        with patch.object(service, 'call_agent', side_effect=ValueError('PRIVATE RESPONSE')) as call:
            with self.assertRaises(ValueError):
                service.refine_text(state, [])
        self.assertEqual(call.call_count, 2)
        self.assertNotIn('PRIVATE', json.dumps(state['gathered']))

    def test_network_failure_is_not_a_format_retry(self):
        with patch.object(service, 'call_agent', side_effect=TimeoutError()) as call:
            with self.assertRaises(TimeoutError):
                service.refine_text(self.state(), [])
        self.assertEqual(call.call_count, 1)

    def test_script_handoff_remains_direction_only_and_source_unchanged(self):
        state = self.state(True)
        original = state['raw_brief']
        with patch.object(service, 'call_agent', return_value={'production_direction': {k: 'Preserve supplied intent.' for k in service.DIRECTION_FIELDS}}):
            result = service.refine_text(state, [])
        self.assertEqual(state['raw_brief'], original)
        self.assertNotIn(original, result)
        self.assertIn('Visual execution:', result)

    def test_all_presets_and_contracts_reject_wrong_types(self):
        for mode in ('character', 'product', 'cgi', 'ugc'):
            system = refinement_system(mode)
            _, schema = contract_for(system, json.dumps(self.state()))
            with self.assertRaises(ValueError):
                validate({'refined_prompt': ['bad']}, schema)
            self.assertIn('PRESET:', system)


if __name__ == '__main__':
    unittest.main()
