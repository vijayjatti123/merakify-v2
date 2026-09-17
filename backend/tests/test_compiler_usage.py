"""Compiler telemetry must survive parsing failures without worker DB writes."""
import json
import copy
import queue
import threading
import time
import unittest
from unittest.mock import patch

from app.services import shot_prompt_compiler as compiler
from test_shot_prompt_compiler import source, creative_prose as prose


class CompilerUsageTests(unittest.TestCase):
    def test_model_gets_optical_purpose_not_copyable_hardware_tag(self):
        original = source()
        before = copy.deepcopy(original)
        captured = []
        def provider(system, content, **kwargs):
            captured.append(json.loads(content))
            return {'shots': [{'shot_number': 1, 'compiled_prompt': prose()}]}
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}):
            compiler.compile_shot_prompts(original, emit=lambda *a: None, call_agent=provider)
        item = captured[0]['shots'][0]
        self.assertEqual(item['hardware_reference'], 'Arri Alexa')
        self.assertNotIn('hardware_language', item)
        self.assertNotIn('tonal latitude', item['hardware_optical_intent'])
        self.assertEqual(original, before)
        validation = compiler.compiler_input(original, emit=lambda *a: None)
        self.assertEqual(validation['shots'][0]['hardware_language'], 'Arri Alexa tonal latitude')

    def test_two_batches_share_one_hard_deadline_and_late_output_cannot_persist(self):
        result = source()
        result['shots'].append({**result['shots'][0], 'shot_number': 2})
        result['assembly']['transitions'] = [{'between':'1-2', 'type':'cut'}]
        original = copy.deepcopy(result)
        events, calls = [], []
        def provider(system, content, **kwargs):
            data = json.loads(content)
            correction = 'input' in data
            data = data.get('input', data)
            calls.append((data['shots'][0]['shot_number'], correction, kwargs['request_timeout']))
            time.sleep(.07 if correction else .01)
            return {'shots':[{'shot_number':data['shots'][0]['shot_number'],
                              'compiled_prompt':prose() if correction else '"unexpected dialogue"'}]}
        start = time.monotonic()
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}), patch.object(compiler,'COMPILER_BATCH_SIZE',1), patch.object(compiler,'COMPILER_DEADLINE_SEC',.12), patch.object(compiler,'validate_compiled',return_value=[]):
            with self.assertRaisesRegex(TimeoutError,'deadline exceeded'):
                compiler.compile_shot_prompts(result,emit=lambda k,n:events.append((k,n)),call_agent=provider)
        elapsed = time.monotonic()-start
        self.assertLess(elapsed,.2)
        self.assertEqual([(n,c) for n,c,t in calls],[(1,False),(2,False),(1,True),(2,True)])
        self.assertLess(calls[-1][2],.06)
        at_timeout = list(events)
        time.sleep(.1)
        self.assertEqual(events,at_timeout)
        self.assertEqual(result,original)

    def test_lookahead_deadline_uses_completion_time_not_dequeue_time(self):
        deadline = time.monotonic() - 1
        completed = queue.Queue()
        completed.put((True, {'shots': []}, deadline - 0.1))
        self.assertEqual(compiler._await_provider(completed, deadline=deadline), {'shots': []})
        completed.put((True, {'shots': []}, deadline + 0.1))
        with self.assertRaisesRegex(TimeoutError, 'late provider'):
            compiler._await_provider(completed, deadline=deadline)

    def test_truncated_then_valid_usage_emits_on_orchestrating_thread(self):
        owner = threading.get_ident()
        events, calls = [], []
        def emit(key, note):
            self.assertEqual(threading.get_ident(), owner)
            events.append((key, note))
        def provider(system, content, **kwargs):
            self.assertNotEqual(threading.get_ident(), owner)
            calls.append(kwargs)
            first = len(calls) == 1
            kwargs['on_response']({
                'stop_reason': 'max_tokens' if first else 'end_turn',
                'usage': {'output_tokens': 16000 if first else 900},
                'visible_text_chars': 15 if first else len(prose()),
                'max_tokens': kwargs['max_tokens'], 'will_retry': False,
            })
            if first:
                raise ValueError('Model response was cut off at the 16000-token limit')
            return {'shots': [{'shot_number': 1, 'compiled_prompt': prose()}]}
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}):
            compiler.compile_shot_prompts(source(), emit=emit, call_agent=provider)
        usage = [json.loads(note) for key, note in events if key == 'shot_prompt_compiler_usage']
        self.assertEqual([u['stop_reason'] for u in usage], ['max_tokens', 'end_turn'])
        self.assertEqual([u['compiler_attempt'] for u in usage], [1, 2])
        self.assertEqual([u['shot_numbers'] for u in usage], [[1], [1]])
        self.assertTrue(all(u['provider_elapsed_sec'] >= 0 for u in usage))
        self.assertEqual([c['max_tokens'] for c in calls], [compiler.compiler_token_budget(1), 32768])
        self.assertIn('output_token_limit', str(events))

    def test_format_categories_do_not_confuse_timeout_with_json(self):
        self.assertEqual(compiler._format_failure(ValueError('Model response was not valid JSON (delimiter)')), 'invalid_json')
        self.assertEqual(compiler._format_failure(ValueError('No JSON object found in model response.')), 'missing_json_object')
        self.assertIsNone(compiler._format_failure(TimeoutError('deadline exceeded')))

    def test_batch_budget_scales_but_does_not_stack_retries(self):
        self.assertEqual(compiler.compiler_token_budget(2), 18432)
        self.assertEqual(compiler.compiler_token_budget(4), 20480)
        self.assertEqual(compiler.compiler_token_budget(100), 24576)
        self.assertEqual(compiler._compiler_time_budget([[{}]*4, [{}]*2]), 300)
        self.assertEqual(compiler._compiler_time_budget([[{}]*4]*20), 300)
        calls = []
        def provider(system, content, **kwargs):
            calls.append(kwargs['max_tokens'])
            raise ValueError('Model response was cut off at token limit')
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}):
            with self.assertRaisesRegex(ValueError, 'cut off'):
                compiler.compile_shot_prompts(source(), emit=lambda *a: None, call_agent=provider)
        self.assertEqual(calls, [17408, 32768])

    def test_valid_json_validation_retry_does_not_raise_token_budget(self):
        calls = []
        def provider(system, content, **kwargs):
            calls.append(kwargs['max_tokens'])
            return {'shots': []}
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}):
            with self.assertRaisesRegex(ValueError, 'validation'):
                compiler.compile_shot_prompts(source(), emit=lambda *a: None, call_agent=provider)
        self.assertEqual(calls, [17408, 17408])

    def test_unclaimed_completed_batch_usage_survives_other_batch_failure(self):
        result = source()
        result['shots'].append({**result['shots'][0], 'shot_number': 2})
        result['assembly']['transitions'] = [{'between': '1-2', 'type': 'cut'}]
        ready = threading.Event()
        events = []
        def provider(system, content, **kwargs):
            number = json.loads(content)['shots'][0]['shot_number']
            if number == 1:
                self.assertTrue(ready.wait(1))
                raise RuntimeError('provider unavailable')
            kwargs['on_response']({'stop_reason': 'end_turn', 'usage': {'output_tokens': 800}})
            ready.set()
            return {'shots': [{'shot_number': number, 'compiled_prompt': prose()}]}
        with patch('app.services.prompt_technique_service.shot_knowledge', return_value={}), patch.object(compiler, 'COMPILER_BATCH_SIZE', 1):
            with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
                compiler.compile_shot_prompts(result, emit=lambda k,n: events.append((k,n)), call_agent=provider)
        usage = [json.loads(n) for k,n in events if k == 'shot_prompt_compiler_usage']
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]['batch'], 2)
        self.assertTrue(usage[0]['discarded'])
