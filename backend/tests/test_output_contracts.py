import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.agents import output_contracts as contracts, llm_client, prompts


class OutputContractTests(unittest.TestCase):
    def test_fresh_director_requires_execution_while_legacy_patch_can_preserve_old_fields(self):
        schema=contracts.contracts()['director-v2']['properties']['shots']['items']['properties']['shot_direction']
        value={'purpose':'Show the lift','performance':'Unhurried reach','product_props':'Blue cup', 'edit_intent':'Hold'}
        with self.assertRaises(ValueError): contracts.validate(value,schema)
        value.update(blocking='Hand screen right',action_beats=['Reach','Lift and settle'],critical_outcome='Cup clears table', dialogue_beat_index=0)
        with self.assertRaises(ValueError): contracts.validate(value,schema)  # Old vague staging cannot pass.
        value.update(entry_exit_paths=['Hand: screen right -> cup handle'], support_and_contact='Cup remains supported by table until grasped.',
                     spatial_invariants=['Cup stays above table.'], forbidden_geometry=['Cup must not float.'])
        contracts.validate(value,schema)
        patch_schema=contracts.contracts()['patch-v1']
        contracts.validate({'patches':[{'shot_number':1,'changes':{'shot_direction':value}}]},patch_schema)

    def setUp(self):
        from app.config import settings
        enabled = patch.object(settings, 'planning_structured_outputs', True)
        enabled.start(); self.addCleanup(enabled.stop)

    def test_director_contract_is_mandatory_even_when_legacy_flag_disabled(self):
        from app.config import settings
        with patch.object(settings, 'planning_structured_outputs', False):
            self.assertEqual(contracts.contract_for(prompts.CINEMATOGRAPHY_AGENT, '{}')[0], 'director-v2')
            self.assertEqual(contracts.contract_for(prompts.CINEMATOGRAPHY_PATCH, '{}')[0], 'patch-v1')

    def test_directed_qa_uses_stable_schema_and_still_allows_rejection(self):
        result = {'approved': False, 'issues': [{'shot_number': 2, 'problem': 'Missing second attempt',
                  'fix_instruction': 'Restore the second attempt without changing speech'}],
                  'scene_coverage': [{'scene_number': 1, 'shot_numbers': [1], 'covered': False, 'evidence': 'Only one throw'}]}
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(stop_reason='end_turn',
            content=[SimpleNamespace(type='text', text=json.dumps(result))])
        with patch.object(llm_client, '_client', client):
            self.assertEqual(llm_client.call_agent(prompts.QA_AGENT, '{"ad_direction":{"takeaway":"x"}}'), result)
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request['output_config']['format']['schema'], contracts.contracts()['qa-v1'])
        self.assertEqual(request['model'], llm_client.REASONING_MODEL)
        self.assertEqual(contracts.contract_for(prompts.QA_AGENT, '{"ad_direction":{"takeaway":"different"}}')[1], request['output_config']['format']['schema'])

    def test_missing_fields_and_bool_in_integer_slot_rejected(self):
        schema = contracts.contracts()['qa-v1']
        for value in ({'approved': True}, {'approved': True, 'issues': [], 'scene_coverage': [
                {'scene_number': True, 'shot_numbers': [1], 'covered': True, 'evidence': 'x'}]}):
            with self.assertRaises(ValueError): contracts.validate(value, schema)

    def test_legacy_and_other_agents_unchanged(self):
        self.assertEqual(contracts.contract_for(prompts.QA_AGENT, 'legacy prose'), (None, None))
        self.assertEqual(contracts.contract_for(prompts.SHOT_PROMPT_COMPILER, '{}'), (None, None))
        self.assertEqual(contracts.contract_for(prompts.CINEMATOGRAPHY_FIX, '{}'), (None, None))

    def test_required_evidence_schema_even_without_optional_director_schema(self):
        from app.config import settings
        with patch.object(settings, 'planning_structured_outputs', False):
            name, schema = contracts.contract_for(prompts.QA_AGENT,
                '{"ad_direction":{"takeaway":"x"},"requirements":[{"id":"s1:heading:1"}]}')
        self.assertEqual(name, 'qa-v2')
        with self.assertRaises(ValueError):
            contracts.validate({'approved': True, 'scene_coverage': [], 'issues': []}, schema)
        valid = {'approved': True, 'requirement_coverage': [], 'shot_checks': [], 'issues': []}
        contracts.validate(valid, schema)
        with self.assertRaises(ValueError):
            contracts.validate({**valid, 'scene_coverage': []}, schema)

    def test_patch_contract_forbids_dialogue_and_unknown_fields(self):
        schema = contracts.contracts()['patch-v1']
        valid = {'patches': [{'shot_number': 1, 'changes': {'camera_angle': 'low angle wide'}}]}
        contracts.validate(valid, schema)
        invalid = copy.deepcopy(valid); invalid['patches'][0]['changes']['dialogue_text'] = 'Changed'
        with self.assertRaises(ValueError): contracts.validate(invalid, schema)

    def test_refusal_is_not_parsed_or_silently_fallen_back(self):
        client = Mock(); client.messages.create.return_value = SimpleNamespace(
            stop_reason='refusal', content=[SimpleNamespace(type='text', text='No')])
        with patch.object(llm_client, '_client', client), self.assertRaisesRegex(ValueError, 'provider could not'):
            llm_client.call_agent(prompts.QA_AGENT, '{}')
        self.assertEqual(client.messages.create.call_count, 1)
