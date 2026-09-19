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
  shot={'shot_number':1,'duration_sec':16,'has_dialogue':False,'dialogue_text':'','characters_in_shot':['Invented']}
  result=check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}]);self.assertFalse(result['approved']);self.assertIn('15 seconds',result['issues'][0]['problem']);self.assertIn('exact names',result['issues'][0]['problem'])
 def test_long_complete_onscreen_dialogue_still_allowed(self):
  shot={'shot_number':1,'duration_sec':12,'has_dialogue':True,'speech_mode':'onscreen','dialogue_text':'A complete long line.','characters_in_shot':['Meera']}
  self.assertTrue(check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}])['approved'])
 def test_model_floor_is_mandatory_and_does_not_change_dialogue(self):
  shot={'shot_number':1,'duration_sec':2.5,'has_dialogue':True,'speech_mode':'onscreen','dialogue_text':'Keep this exact line.','characters_in_shot':['Meera']}
  self.assertFalse(check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}])['approved'])
  checked=check_mechanics({'approved':True,'issues':[]},[shot],[{'name':'Meera'}],4)
  self.assertFalse(checked['approved'])
  self.assertEqual(shot['dialogue_text'],'Keep this exact line.')
  self.assertEqual(shot['duration_sec'],2.5)
 def test_single_twelve_second_silent_shot_is_supported(self):
  shot={'shot_number':1,'duration_sec':12,'has_dialogue':False,'dialogue_text':'','characters_in_shot':[]}
  self.assertTrue(check_mechanics({'approved':True,'issues':[]},[shot],[])['approved'])
 def test_speech_never_shrinks_performance(self):
  from app.services.dialogue_duration import performance_duration
  for speech in (2.56, 1.7066666667, .8533333333):
   self.assertEqual(performance_duration(4, speech), 5)
  self.assertEqual(performance_duration(12, 2.56), 12)
  self.assertEqual(performance_duration(4, 6), 6)
  with self.assertRaises(ValueError): performance_duration(float('nan'), 2)

 def test_five_second_floor_overrides_legacy_four_second_contract(self):
  for mode in ("none", "onscreen", "voiceover"):
   shot={"shot_number":1,"duration_sec":4.99,"has_dialogue":mode!="none","speech_mode":mode,
         "dialogue_text":"Keep the words." if mode!="none" else "",
         "characters_in_shot":["Meera"] if mode=="onscreen" else []}
   for legacy_floor in (None,4,5):
    self.assertFalse(check_mechanics({"approved":True,"issues":[]},[shot],[{"name":"Meera"}],legacy_floor)["approved"])
   shot["duration_sec"]=5
   self.assertTrue(check_mechanics({"approved":True,"issues":[]},[shot],[{"name":"Meera"}])["approved"])
if __name__=='__main__' :unittest.main()
