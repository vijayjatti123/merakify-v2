import copy
import json
import unittest
from unittest.mock import Mock, patch
from app.services import camera_direction as camera
from app.services import shot_prompt_compiler as compiler
from test_shot_prompt_compiler import source, creative_prose


class CameraDirectionTests(unittest.TestCase):
    def test_existing_cinematography_vocabulary(self):
        for raw in ['dollyin', 'dollyout', 'tracking', 'orbit', 'handheld', 'craneup', 'cranedown',
                    'slow push-in', 'slow pull out', 'slow slide in', 'slow pan right', 'slow tilt up',
                    'slow zoom in', 'slow lateral dolly', 'subtle handheld drift', 'whip pan upward', '   ']:
            with self.subTest(raw=raw):
                self.assertTrue(camera.render(camera.normalize_legacy(raw)))

    def test_barista_instructions(self):
        cases = [('Static, slow steam drift', 'hold', 'locked'),
                 ('Slow push in on swirl', 'dolly', 'smooth'),
                 ('Static, gentle handheld sway', 'hold', 'handheld')]
        for raw, move, support in cases:
            spec = camera.normalize_legacy(raw)
            self.assertEqual((spec['movement'], spec['stabilization']), (move, support))
        self.assertIn('dolly forward slowly', camera.render(camera.normalize_legacy(cases[1][0])))

    def test_every_supported_family_renders_without_provider_tokens(self):
        for move, directions in camera.ALLOWED.items():
            for direction in directions:
                spec = dict(movement=move, direction=direction,
                            speed='none' if move == 'hold' else 'slow',
                            stabilization='locked' if move == 'hold' else 'smooth')
                sentence = camera.render(spec)
                self.assertTrue(sentence.startswith('Camera direction:'))
                self.assertNotIn('@', sentence)

    def test_unknown_and_contradictory_legacy_inputs_are_not_silently_static(self):
        for raw in ['cinematic magic', 'static push in', 'pan left then tilt up', 'slow dolly zoom']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                camera.normalize_legacy(raw)

    def test_invalid_structured_combinations(self):
        for changes in [dict(direction='up'), dict(speed='whip'), dict(stabilization='locked'), dict(movement='invented')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                camera.validate(dict(dict(movement='dolly', direction='in', speed='slow', stabilization='smooth'), **changes))

    def test_prose_guard_keeps_subject_motion_but_rejects_camera_negation(self):
        self.assertFalse(camera.conflicting_prose('Steam drifts while hands tilt the jug.', ''))
        self.assertFalse(camera.conflicting_prose('At a low camera angle the hands tilt the jug.', ''))
        self.assertTrue(camera.conflicting_prose('The camera does not push in; it pulls away.', ''))
        self.assertTrue(camera.conflicting_prose('The camera slowly pushes in toward swirling milk.', ''))
        self.assertTrue(camera.conflicting_prose('Slowly push in toward the cup.', ''))

    def test_structured_facts_do_not_require_matching_summary_words(self):
        spec = dict(movement='dolly', direction='in', speed='slow', stabilization='smooth')
        self.assertEqual(camera.for_shot({'camera_direction': spec, 'camera_movement': 'Approach the latte gently'}), spec)

    @patch('app.services.prompt_technique_service.shot_knowledge', return_value={})
    def test_actual_compiler_inserts_camera_once_without_model_copying_it(self, knowledge):
        job = source(); job['shots'][0]['camera_movement'] = 'Slow push in on swirl'
        original = copy.deepcopy(job)
        call = Mock(return_value={'shots': [{'shot_number': 1, 'compiled_prompt': creative_prose()}]})
        result = compiler.compile_shot_prompts(job, emit=Mock(), call_agent=call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(job, original)
        text = result[0]['compiled_prompt']
        self.assertEqual(text.count('Camera direction:'), 1)
        self.assertIn('dolly forward slowly', text)
        sent = json.loads(call.call_args.args[1])
        self.assertEqual(sent['shots'][0]['camera_direction']['direction'], 'in')
        self.assertGreater(sent['shots'][0]['programmatic_reserved_words'], len(compiler.TEXT_GUARD.split()))

    @patch('app.services.prompt_technique_service.shot_knowledge', return_value={})
    def test_conflicting_model_camera_still_gets_bounded_rejection(self, knowledge):
        bad = creative_prose().replace('At the given wide angle', 'The camera pulls away at the given wide angle')
        call = Mock(return_value={'shots': [{'shot_number': 1, 'compiled_prompt': bad}]})
        with self.assertRaisesRegex(ValueError, 'camera behavior belongs only'):
            compiler.compile_shot_prompts(source(), emit=Mock(), call_agent=call)
        self.assertEqual(call.call_count, 2)

    def test_angle_lens_and_composition_are_preserved(self):
        shot = dict(camera_angle='Low-angle extreme close-up', lens='85mm, rack focus to cup',
                    composition_note='cup left', camera_movement='slow push in')
        original = copy.deepcopy(shot)
        camera.for_shot(shot)
        self.assertEqual(shot, original)

    def test_zoom_and_dolly_are_distinct(self):
        self.assertNotEqual(camera.normalize_legacy('slow zoom in'), camera.normalize_legacy('slow push in'))
        self.assertIn('without travelling', camera.render(camera.normalize_legacy('slow zoom in')))

    def test_invalid_camera_enters_existing_qa_fix_loop(self):
        from app.agents import director, prompts
        job = source(); shot = job['shots'][0]
        shot.update(camera_movement='static push in', has_dialogue=True, dialogue_text='Keep this complete line.', duration_sec=5)
        fixed = {**shot, 'camera_movement': 'slow push in', 'camera_direction':
                 dict(movement='dolly', direction='in', speed='slow', stabilization='smooth')}
        call = Mock(side_effect=[{'approved': True, 'issues': []},
                                {'patches': [{'shot_number': shot['shot_number'], 'changes': {'camera_direction': fixed['camera_direction']}}]},
                                {'approved': True, 'issues': []}])
        with patch.object(director, 'call_agent', call):
            result = director.validate_and_correct([shot], [], 5, source_script_text='Keep this complete line.')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(call.call_args_list[1].args[0], prompts.CINEMATOGRAPHY_PATCH)
        self.assertTrue(result['qa']['approved'])
        self.assertEqual(result['shots'][0]['dialogue_text'], shot['dialogue_text'])
        self.assertEqual(result['shots'][0]['description'], shot['description'])
        self.assertEqual(result['shots'][0]['camera_direction'], fixed['camera_direction'])

    @patch('app.services.prompt_technique_service.shot_knowledge', return_value={})
    def test_corrective_retry_only_rewrites_failed_shot(self, knowledge):
        job = source(); job['shots'].append({**job['shots'][0], 'shot_number': 2, 'description': 'Copper kettle'})
        job['assembly']['transitions'] = [{'between': '1-2', 'type': 'cut'}]
        second = ('Copper kettle presents its curved outline clearly against the available background, its handle remaining in the established position. '
                  'Render style: natural rendering follows the blue palette, with soft highlights describing the metal rather than inventing markings or decoration. '
                  'The wide composition leaves breathing room around the vessel; window illumination separates its rounded body from the darker surroundings, and the 70mm anamorphic reference preserves subtle changes between reflected brightness and shaded copper, allowing attention to rest on material and shape for the duration of this quiet moment without adding another action. ' + compiler.TEXT_GUARD)
        call = Mock(side_effect=[{'shots': [{'shot_number': 1, 'compiled_prompt': creative_prose()}, {'shot_number': 2, 'compiled_prompt': 'Too short'}]},
                                {'shots': [{'shot_number': 2, 'compiled_prompt': second}]}])
        result = compiler.compile_shot_prompts(job, emit=Mock(), call_agent=call)
        retry = json.loads(call.call_args.args[1])
        self.assertEqual([s['shot_number'] for s in retry['input']['shots']], [2])
        self.assertEqual(retry['input']['readonly_compiled_shots'][0]['compiled_prompt'], creative_prose())
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0]['compiled_prompt'].startswith(creative_prose().removesuffix(compiler.TEXT_GUARD).rstrip()))
        checkpoints = []
        failing = Mock(side_effect=[{'shots': [{'shot_number': 1, 'compiled_prompt': creative_prose()}, {'shot_number': 2, 'compiled_prompt': 'Too short'}]},
                                    {'shots': [{'shot_number': 2, 'compiled_prompt': 'Still too short'}]}])
        with self.assertRaises(ValueError):
            compiler.compile_shot_prompts(job, emit=Mock(), call_agent=failing, on_checkpoint=checkpoints.append)
        self.assertEqual([s['shot_number'] for s in checkpoints[-1]['shots']], [1])
        job['video_prompt_checkpoint'] = checkpoints[-1]
        resume = Mock(return_value={'shots': [{'shot_number': 2, 'compiled_prompt': second}]})
        resumed = compiler.compile_shot_prompts(job, emit=Mock(), call_agent=resume)
        self.assertEqual([s['shot_number'] for s in json.loads(resume.call_args.args[1])['shots']], [2])
        self.assertEqual(resumed[0]['compiled_prompt'], result[0]['compiled_prompt'])

    @patch('app.services.prompt_technique_service.shot_knowledge', return_value={})
    def test_checkpoint_is_invalidated_when_camera_changes(self, knowledge):
        job = source(); checkpoints = []
        call = Mock(return_value={'shots': [{'shot_number': 1, 'compiled_prompt': creative_prose()}]})
        compiler.compile_shot_prompts(job, emit=Mock(), call_agent=call, on_checkpoint=checkpoints.append)
        job['video_prompt_checkpoint'] = checkpoints[-1]
        unused = Mock(side_effect=AssertionError('Cached prompt should not need a provider'))
        compiler.compile_shot_prompts(job, emit=Mock(), call_agent=unused)
        unused.assert_not_called()
        job['shots'][0]['camera_movement'] = 'slow push in'
        call.reset_mock()
        compiler.compile_shot_prompts(job, emit=Mock(), call_agent=call)
        call.assert_called_once()
