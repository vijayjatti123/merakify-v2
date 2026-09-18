import copy
import unittest
from unittest.mock import patch

from app.agents import prompts
from app.agents.director import validate_and_correct
from app.agents.dialogue_integrity import screen_issues, protected_dialogue


def shots(two=False):
    return [dict(shot_number=1,scene_number=2,duration_sec=5,has_dialogue=True,dialogue_text='Time to go.',characters_in_shot=[]),
            dict(shot_number=2,scene_number=2,duration_sec=5,has_dialogue=two,dialogue_text='Come along.' if two else '',characters_in_shot=[])]


def issue(number=1,problem='Scene 2 has two shots with dialogue',instruction='Remove dialogue from shot 1, make silent cutaway'):
    return {'shot_number':number,'problem':problem,'fix_instruction':instruction}


class DialogueIntegrityTests(unittest.TestCase):
    def test_false_count_discarded_without_touching_script(self):
        protected = protected_dialogue(shots(), 'Nila: Time to go.')
        accepted, _, rejected, _ = screen_issues(shots(), [issue()], protected, lambda *_: None)
        self.assertFalse(accepted)
        self.assertTrue(rejected)

    def test_protected_script_line_removal_stays_blocked(self):
        protected = protected_dialogue(shots(True), 'Nila: Time to go. Nila: Come along.')
        accepted, _, _, limitations = screen_issues(shots(True), [issue()], protected, lambda *_: None)
        self.assertFalse(accepted)
        self.assertTrue(limitations)

    def test_adversarial_repair_cannot_alter_or_remove_protected_shot(self):
        from app.agents.dialogue_integrity import restore_protected
        before = shots()
        for after in [before[1:], [{**before[0], 'dialogue_text':'Leave now.'}, before[1]]]:
            restored, violations = restore_protected(before, after, protected_dialogue(before, 'Nila: Time to go.'), lambda *_: None)
            self.assertEqual(restored[0]['dialogue_text'], 'Time to go.')
            self.assertTrue(violations)

    def test_unexpected_dialogue_loss_remains_detectable(self):
        from app.agents.dialogue_integrity import warn_dialogue_loss
        after = shots(); after[0].update(has_dialogue=False, dialogue_text='')
        self.assertTrue(warn_dialogue_loss(shots(), after, set(), lambda *_: None))

    def test_user_review_does_not_call_model_or_delete_dialogue(self):
        with patch('app.agents.director.call_agent', side_effect=AssertionError('No model review')):
            result = validate_and_correct(shots(True), [], 10, source_script_text='Time to go. Come along.')
        self.assertEqual([s['dialogue_text'] for s in result['shots']], ['Time to go.', 'Come along.'])
        self.assertFalse(result['qa']['semantic_review_performed'])

    def test_count_wording_and_wrong_numeric_claim(self):
        for text in ['Scene 2 has multiple dialogue shots','Two dialogue shots in scene 2','Dialogue split across shots in scene 2','Scene 2 has dialogue shots: 2','Second shot in scene 2 has dialogue']:
            accepted,_,rejected,_=screen_issues(shots(),[issue(problem=text)],{},lambda *_:None)
            self.assertFalse(accepted,text);self.assertTrue(rejected,text)
        accepted,_,rejected,_=screen_issues(shots(True),[issue(problem='Scene 2 has three dialogue shots')],{},lambda *_:None)
        self.assertFalse(accepted);self.assertTrue(rejected)
        accepted,verified,rejected,_=screen_issues(shots(True),[issue(problem='Second shot in scene 2 has dialogue')],{},lambda *_:None)
        self.assertTrue(accepted);self.assertEqual(verified,{1});self.assertFalse(rejected)

    def test_non_count_dialogue_issue_not_misclassified(self):
        finding=issue(problem='Scene 2 dialogue uses wrong speaker',instruction='Use narrator reference')
        accepted,_,rejected,_=screen_issues(shots(),[finding],{},lambda *_:None)
        self.assertEqual(accepted,[finding]);self.assertFalse(rejected)

    def test_source_match_tolerates_punctuation_not_partial_words(self):
        self.assertTrue(protected_dialogue(shots(),'Nila: “Time to go!”'))
        self.assertFalse(protected_dialogue(shots(),'Time to goad the crowd'))
