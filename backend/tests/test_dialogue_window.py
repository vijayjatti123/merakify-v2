import unittest

from app.services import dialogue_window


class DialogueWindowTests(unittest.TestCase):
    def test_director_beat_places_late_dialogue_near_visible_delivery(self):
        shot = {'duration_sec':7, 'shot_direction':{
            'action_beats':['Kabir trembles.', 'Tara hands over the bottle.',
                            'Tara speaks the approved line.'], 'dialogue_beat_index':3}}
        timing = dialogue_window.calculate(shot, 2.133333)
        self.assertEqual((timing['beat_index'], timing['source']), (3, 'director'))
        self.assertTrue(4.6 < timing['start_sec'] < 5.0)
        self.assertTrue(6.7 < timing['end_sec'] <= 7)

    def test_legacy_unique_speech_cue_is_safe_but_ambiguous_plan_is_blocked(self):
        legacy = {'duration_sec':7, 'shot_direction':{'action_beats':[
            'Kabir hesitates.', 'Tara hands over the bottle.', 'Tara speaks reassuringly.']}}
        self.assertEqual(dialogue_window.calculate(legacy, 2)['source'], 'legacy_action_beat')
        ambiguous = {'duration_sec':7, 'shot_direction':{'action_beats':['They wait.', 'They smile.']}}
        with self.assertRaisesRegex(ValueError, 'timing is missing'):
            dialogue_window.calculate(ambiguous, 2)

    def test_prompt_directs_one_integrated_performance_without_audio_overlay(self):
        shot = {'duration_sec':5, 'dialogue_audio_duration_sec':2,
                'shot_direction':{'action_beats':['Speak the line.', 'Settle.'], 'dialogue_beat_index':1}}
        instruction = dialogue_window.prompt_instruction(shot, reference_label='Audio 1')
        timing = dialogue_window.from_shot(shot)
        self.assertIn('PERFORMANCE TIMELINE', instruction)
        self.assertIn(f"begins only at {timing['start_sec']:.3f}s", instruction)
        self.assertIn('0.00-2.50s: Speak the line.', instruction)

    def test_middle_delivery_beat_has_action_before_and_reaction_after(self):
        shot = {'duration_sec': 6, 'dialogue_audio_duration_sec': 2.133333,
                'shot_direction': {'action_beats': [
                    'Tara approaches while Kabir hesitates.',
                    'Tara speaks the approved line.',
                    'Kabir settles and looks confident.'],
                    'dialogue_beat_index': 2}}
        timing = dialogue_window.from_shot(shot)
        self.assertEqual(timing['start_sec'], 1.933)
        self.assertEqual(timing['end_sec'], 4.067)
        instruction = dialogue_window.prompt_instruction(shot)
        self.assertIn('1.93-4.07s: Tara speaks the approved line.', instruction)
        self.assertIn('4.07-6.00s: Kabir settles and looks confident.', instruction)
        self.assertIn(f"finishes by {timing['end_sec']:.3f}s", instruction)
        self.assertIn('voice, accent and pronunciation reference only', instruction)
        self.assertIn('waveform must not be laid over the beginning', instruction)
        self.assertIn('Generate the voice and mouth performance together', instruction)

    def test_exact_audio_quotes_line_and_silences_everyone_outside_its_window(self):
        shot = {'duration_sec': 6, 'dialogue_audio_duration_sec': 2.133,
                'speech_mode': 'onscreen', 'speaker_label': 'Tara',
                'dialogue_text': 'Drink this, have confidence.',
                'shot_direction': {'action_beats': [
                    'Kabir hesitates while Tara offers the bottle.',
                    'Tara speaks the approved line.',
                    'Kabir accepts the bottle.'], 'dialogue_beat_index': 2}}
        prompt = dialogue_window.prompt_instruction(shot, exact_audio=True, speaker='Tara')
        self.assertIn('Tara says exactly once: "Drink this, have confidence."', prompt)
        self.assertIn('everyone is silent', prompt)
        self.assertIn('only audio authority', prompt)
        self.assertLess(prompt.index('Tara offers'), prompt.index('Tara speaks'))
        self.assertLess(prompt.index('Tara speaks'), prompt.index('Kabir accepts'))
        self.assertNotIn('pronunciation reference only', prompt)

    def test_visual_direction_omits_missing_legacy_fields(self):
        prompt = dialogue_window.visual_instruction({}, {'description': 'Kabir pauses.'},
                                                      'the accepted preview', speaking=False)
        self.assertIn('Kabir pauses.', prompt)
        self.assertNotIn('Camera:', prompt)
        self.assertNotIn('Lighting:', prompt)
        self.assertNotIn('lens .', prompt)

    def test_saved_old_window_cannot_override_edited_action_beats(self):
        shot = {'duration_sec': 6, 'dialogue_audio_duration_sec': 1,
                'dialogue_timing': {'start_sec': 0, 'end_sec': 1},
                'video_dialogue_timing': {'start_sec': 0, 'end_sec': 1},
                'shot_direction': {'dialogue_beat_index': 3,
                    'action_beats': ['Offer the bottle.', 'Kabir takes it.', 'Tara speaks.']}}
        timing = dialogue_window.from_shot(shot, require=True)
        self.assertGreater(timing['start_sec'], 4)

    def test_speaking_beat_cannot_hide_prior_action(self):
        shot = {'duration_sec': 6, 'dialogue_audio_duration_sec': 2,
                'shot_direction': {'dialogue_beat_index': 2,
                    'action_beats': ['Tara offers a bottle.',
                                     'Tara speaks only after Kabir finishes drinking.',
                                     'Kabir smiles.']}}
        with self.assertRaisesRegex(ValueError, 'speaking beat also depends'):
            dialogue_window.from_shot(shot, require=True)

    def test_visual_direction_carries_locked_style_and_exit_path(self):
        result = {'continuity': {'visual_style': {'rendering': 'Natural live action',
                                                  'palette': 'Warm daylight'}}}
        shot = {'state_at_shot_start': 'Kabir stands inside the cabin.',
                'shot_direction': {'entry_exit_paths': ['Kabir: cabin floor -> door -> clear air'],
                                   'support_and_contact': 'Feet on the cabin floor.'}}
        prompt = dialogue_window.visual_instruction(result, shot, 'the accepted preview')
        self.assertIn('Natural live action', prompt)
        self.assertIn('cabin floor -> door -> clear air', prompt)
        self.assertIn('Feet on the cabin floor.', prompt)
        self.assertNotIn('floor..', prompt)


if __name__ == '__main__':
    unittest.main()
