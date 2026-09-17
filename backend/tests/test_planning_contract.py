import unittest
from app.services.planning_contract import *
class PlanningContractTests(unittest.TestCase):
 def test_reference_projection_preserves_facts_not_urls(self):
  original={'name':'Meera','description':'Locked sari and glasses','image_url':'private','voice_id':'voice'}
  self.assertEqual(creative_references([original]),[{'name':'Meera','description':'Locked sari and glasses'}])
  self.assertEqual(original['image_url'],'private')
 def test_camera_summary_is_owned_by_code_without_changing_action(self):
  shot={'description':'Steam rises','camera_direction':{'movement':'hold','direction':'none','speed':'none','stabilization':'handheld'}}
  render_camera_summaries([shot]);self.assertIn('handheld',shot['camera_movement']);self.assertEqual(shot['description'],'Steam rises')
 def test_invalid_camera_not_silently_replaced(self):
  shot={'camera_direction':{'movement':'unknown'}};render_camera_summaries([shot]);self.assertNotIn('camera_movement',shot)
 def test_caps_and_exact_names_feed_qa(self):
  shot={'shot_number':1,'duration_sec':10,'has_dialogue':False,'dialogue_text':'','characters_in_shot':['Invented']}
  result=check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}]);self.assertFalse(result['approved']);self.assertIn('9-second',result['issues'][0]['problem']);self.assertIn('exact names',result['issues'][0]['problem'])
 def test_long_complete_onscreen_dialogue_still_allowed(self):
  shot={'shot_number':1,'duration_sec':12,'has_dialogue':True,'speech_mode':'onscreen','dialogue_text':'A complete long line.','characters_in_shot':['Meera']}
  self.assertTrue(check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}])['approved'])
 def test_model_floor_is_opt_in_and_does_not_change_dialogue(self):
  shot={'shot_number':1,'duration_sec':2.5,'has_dialogue':True,'speech_mode':'onscreen','dialogue_text':'Keep this exact line.','characters_in_shot':['Meera']}
  self.assertTrue(check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}])['approved'])
  checked=check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}],4)
  self.assertFalse(checked['approved'])
  self.assertEqual(shot['dialogue_text'],'Keep this exact line.')
  self.assertEqual(shot['duration_sec'],2.5)
if __name__=='__main__':unittest.main()
