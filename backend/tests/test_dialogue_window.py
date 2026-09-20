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
        self.assertIn(f"finishes by {timing['end_sec']:.3f}s", instruction)
        self.assertIn('voice, accent and pronunciation reference only', instruction)
        self.assertIn('waveform must not be laid over the beginning', instruction)
        self.assertIn('Generate the voice and mouth performance together', instruction)


if __name__ == '__main__':
    unittest.main()
