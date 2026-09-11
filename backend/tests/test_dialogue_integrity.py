import copy
import unittest
from unittest.mock import patch

from app.agents import prompts
from app.agents.director import validate_and_correct
from app.agents.dialogue_integrity import screen_issues, protected_dialogue


def shots(two=False):
    return [dict(shot_number=1,scene_number=2,has_dialogue=True,dialogue_text='Time to go.',characters_in_shot=[]),
            dict(shot_number=2,scene_number=2,has_dialogue=two,dialogue_text='Come along.' if two else '',characters_in_shot=[])]


def issue(number=1,problem='Scene 2 has two shots with dialogue',instruction='Remove dialogue from shot 1, make silent cutaway'):
    return {'shot_number':number,'problem':problem,'fix_instruction':instruction}


class DialogueIntegrityTests(unittest.TestCase):
    def run_guard(self,before,issues,after=None,source=None):
        events=[]
        seen=[]
        def call(system,user,**kwargs):
            seen.append((system,user))
            if system==prompts.QA_AGENT:
                return {'approved':False,'issues':issues} if sum(s==prompts.QA_AGENT for s,_ in seen)==1 else {'approved':True,'issues':[]}
            if system==prompts.CINEMATOGRAPHY_FIX: return {'shots':copy.deepcopy(after)}
            if system==prompts.SHOT_ASSEMBLER: return {'total_duration_sec':10,'transitions':[]}
            raise AssertionError('Unexpected call')
        with patch('app.agents.director.call_agent',side_effect=call):
            result=validate_and_correct(copy.deepcopy(before),[],10,source_script_text=source,emit=lambda key,note:events.append(note))
        return result,events,seen

    def test_false_count_discarded_without_fix_both_paths(self):
        for source in [None,'Nila: Time to go.']:
            result,events,seen=self.run_guard(shots(),[issue()],source=source)
            self.assertEqual(result['shots'][0]['dialogue_text'],'Time to go.')
            self.assertTrue(result['qa']['approved'])
            self.assertNotIn(prompts.CINEMATOGRAPHY_FIX,[s for s,_ in seen])
            self.assertTrue(any('actual has_dialogue:true count is 1' in e for e in events))

    def test_genuine_violation_forwarded_unchanged(self):
        after=shots(True);after[0].update(has_dialogue=False,dialogue_text='')
        result,events,seen=self.run_guard(shots(True),[issue()],after)
        self.assertEqual(sum(s['has_dialogue'] for s in result['shots']),1)
        self.assertTrue(result['qa']['approved'])
        self.assertFalse(any('dialogue-loss event' in e for e in events))
        self.assertIn('Remove dialogue from shot 1, make silent cutaway',next(u for s,u in seen if s==prompts.CINEMATOGRAPHY_FIX))

    def test_script_line_removal_blocked_and_limitation_retained(self):
        result,events,seen=self.run_guard(shots(True),[issue()],source='Nila: “Time   to go!”\nNila: Come along.')
        self.assertEqual(result['shots'][0]['dialogue_text'],'Time to go.')
        self.assertFalse(result['qa']['approved'])
        self.assertNotIn(prompts.CINEMATOGRAPHY_FIX,[s for s,_ in seen])
        self.assertTrue(any('user-scripted line' in e for e in events))

    def test_unrelated_fix_cannot_alter_or_remove_protected_shot(self):
        for after in [shots()[1:], [{**shots()[0],'dialogue_text':'Leave now.'},shots()[1]]]:
            result,events,seen=self.run_guard(shots(),[issue(problem='Wrong camera angle',instruction='Change shot angle to medium')],after,source='Nila: Time to go.')
            self.assertEqual(result['shots'][0]['dialogue_text'],'Time to go.')
            self.assertFalse(result['qa']['approved'])
            self.assertIn(prompts.CINEMATOGRAPHY_FIX,[s for s,_ in seen])
            self.assertTrue(any('restored the original shot' in e for e in events))

    def test_unexpected_no_script_loss_warns_without_restoring(self):
        after=shots();after[0].update(has_dialogue=False,dialogue_text='')
        result,events,_=self.run_guard(shots(),[issue(problem='Wrong camera angle',instruction='Change shot angle to medium')],after)
        self.assertEqual(result['shots'][0]['dialogue_text'],'')
        self.assertTrue(result['qa']['dialogue_loss_warnings'])
        self.assertTrue(any('count 1 -> 0' in e for e in events))

    def test_verified_reason_does_not_excuse_deleting_other_line(self):
        after=shots(True)
        for shot in after:shot.update(has_dialogue=False,dialogue_text='')
        result,events,_=self.run_guard(shots(True),[issue()],after)
        self.assertTrue(result['qa']['dialogue_loss_warnings'])

    def test_only_false_issue_removed_from_mixed_batch(self):
        angle=issue(number=2,problem='Wrong camera angle',instruction='Use a close-up')
        result,_,seen=self.run_guard(shots(),[issue(),angle],shots())
        request=next(u for s,u in seen if s==prompts.CINEMATOGRAPHY_FIX)
        self.assertNotIn('Scene 2 has two shots with dialogue',request)
        self.assertIn('Wrong camera angle',request)
        self.assertEqual(len(result['qa']['rejected_claims']),1)

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
