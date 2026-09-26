import copy
import unittest

from app.agents.dialogue_integrity import (contains_exact_line, explicit_final_line,
    lock_final_line_in_shots, lock_final_line_in_story)
from app.services import story_requirements


class SourceLineContractTests(unittest.TestCase):
    def test_labelled_hindi_cta_survives_script_and_shot_translation(self):
        brief = 'A glue ad.\n- CTA (Hindi): “अपने काम को दें अटूट बंधन।”'
        line = explicit_final_line(brief)
        self.assertEqual(line, 'अपने काम को दें अटूट बंधन।')
        story = {'scenes': [{'scene_number': 1, 'dialogue_or_vo': 'Give it an unbreakable bond.'}]}
        lock_final_line_in_story(story, line)
        self.assertIn(line, story['scenes'][0]['dialogue_or_vo'])
        # A simple final VO can be corrected without changing camera/staging.
        story['scenes'][0]['dialogue_or_vo'] = line
        shots = [{'shot_number': 1, 'scene_number': 1, 'has_dialogue': True,
                  'dialogue_text': 'Give it an unbreakable bond.', 'camera_angle': 'close-up'}]
        lock_final_line_in_shots(shots, line, 1, story['scenes'][0]['dialogue_or_vo'])
        self.assertEqual(shots[0]['dialogue_text'], line)
        self.assertEqual(shots[0]['camera_angle'], 'close-up')

    def test_unlabelled_marketing_copy_is_not_forced_as_speech(self):
        self.assertIsNone(explicit_final_line('The takeaway is confidence. Show the final pack.'))
        self.assertIsNone(explicit_final_line('Voiceover: “This is a mid-ad line.”'))
        self.assertEqual(explicit_final_line("CTA: “Let's try it today.”"), "Let's try it today.")
        self.assertFalse(contains_exact_line('इसे आज दे', 'इसे आज दें'))

    def test_do_not_overwrite_another_line_in_a_multi_turn_final_scene(self):
        shots = [{'shot_number': 1, 'scene_number': 2, 'has_dialogue': True,
                  'dialogue_text': 'Wait for me.'}]
        before = copy.deepcopy(shots)
        lock_final_line_in_shots(shots, 'Join today.', 2, 'Wait for me.\nJoin today.')
        self.assertEqual(shots, before)

    def test_qa_cannot_approve_paraphrase_of_explicit_final_line(self):
        story = {'scenes': [{'scene_number': 1, 'heading': 'Product',
                  'description': 'The bottle is revealed.', 'dialogue_or_vo': 'Try Bluewater today.'}],
                 'production_context': {'explicit_final_line': 'Try Bluewater today.'}}
        shots = [{'shot_number': 1, 'scene_number': 1, 'dialogue_text': 'Get Bluewater now.'}]
        rows = [{'requirement_id': r['id'], 'shot_numbers': [1], 'covered': True,
                 'evidence': 'Product reveal and spoken CTA'} for r in story_requirements.requirements(story)]
        verdict = {'approved': True, 'issues': [], 'requirement_coverage': rows,
                   'shot_checks': [{'shot_number': 1, 'consistent': True, 'evidence': 'Coherent'}]}
        reviewed = story_requirements.check_review(verdict, shots, story)
        self.assertFalse(reviewed['approved'])
        self.assertEqual(reviewed['issues'][0]['code'], 'missing_exact_final_line')
        self.assertTrue(contains_exact_line('Try Bluewater today!', story['scenes'][0]['dialogue_or_vo']))
        shots[0]['dialogue_text'] = 'Try Bluewater today.'
        self.assertTrue(story_requirements.check_review(verdict, shots, story)['approved'])

    def test_final_line_must_be_spoken_once_at_the_end(self):
        story = {'scenes': [{'scene_number': 1, 'heading': 'Demonstration'},
                            {'scene_number': 2, 'heading': 'Closing'}],
                 'production_context': {'explicit_final_line': 'See it today.'}}
        shots = [{'shot_number': 1, 'scene_number': 1, 'dialogue_text': 'See it today.'},
                 {'shot_number': 2, 'scene_number': 2, 'dialogue_text': ''}]
        rows = [{'requirement_id': r['id'], 'shot_numbers': [r['scene_number']],
                 'covered': True, 'evidence': 'Scene action'} for r in story_requirements.requirements(story)]
        verdict = {'approved': True, 'issues': [], 'requirement_coverage': rows,
                   'shot_checks': [{'shot_number': n, 'consistent': True, 'evidence': 'Coherent'}
                                   for n in (1, 2)]}
        self.assertFalse(story_requirements.check_review(verdict, shots, story)['approved'])
        shots[0]['dialogue_text'] = ''
        shots[1]['dialogue_text'] = 'See it today.'
        self.assertTrue(story_requirements.check_review(verdict, shots, story)['approved'])

    def test_normal_fast_review_checks_the_same_final_line(self):
        from app.services import director_review
        story = {'scenes': [{'scene_number': 1}, {'scene_number': 2}],
                 'production_context': {'explicit_final_line': 'See it today.'}}
        shots = [{'shot_number': 1, 'scene_number': 1, 'description': 'Bottle shown',
                  'camera_angle': 'medium', 'lens': '50mm', 'lighting': 'daylight',
                  'has_dialogue': True, 'speech_mode': 'voiceover', 'dialogue_text': 'See it today.'},
                 {'shot_number': 2, 'scene_number': 2, 'description': 'Ending pack',
                  'camera_angle': 'close-up', 'lens': '85mm', 'lighting': 'daylight',
                  'has_dialogue': True, 'speech_mode': 'voiceover', 'dialogue_text': 'Get it now.'}]
        result = director_review.review(shots, [], approved_story=story)
        self.assertTrue(any('exact final line' in i['problem'] for i in result['issues']))
        shots[0]['dialogue_text'] = ''
        shots[0]['has_dialogue'] = False
        shots[1]['dialogue_text'] = 'See it today.'
        self.assertFalse(any('exact final line' in i['problem']
                             for i in director_review.review(shots, [], approved_story=story)['issues']))


if __name__ == '__main__':
    unittest.main()
