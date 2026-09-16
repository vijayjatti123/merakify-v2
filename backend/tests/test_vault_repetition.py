import copy
import unittest
from unittest.mock import Mock
from app.services import shot_prompt_compiler as compiler
from test_shot_prompt_compiler import source

LOCKED = ('A poised Indian documentary director in her early thirties, with shoulder-length black hair, '
          'a deep teal linen jacket, cream trousers, and a small silver pendant.')
PHRASE = 'an indian documentary director in her early thirties'


class VaultRepetitionTests(unittest.TestCase):
    def case(self, phrase=PHRASE):
        result = source('Seedance 2.0')
        result['continuity']['characters'] = [{'name':'Meera','character_id':'vault-meera',
            'voice_assignment':'vault','description':LOCKED}]
        result['shots'][0]['characters_in_shot'] = ['Meera']
        result['shots'].append({**result['shots'][0], 'shot_number':2,'scene_number':2})
        result['assembly']['transitions'] = [{'between':'1-2','type':'cut','reason':'New scene'}]
        payload = compiler.compiler_input(result, emit=Mock())
        response = {'shots':[{'shot_number':n,'compiled_prompt':phrase+'.'} for n in (1,2)]}
        return payload, response

    def repeated(self, payload, response):
        return [e for e in compiler.validate_compiled(response,payload) if 'repeated descriptive clause' in e]

    def test_real_rejected_identity_phrase_allowed_across_scenes(self):
        p,r=self.case()
        self.assertFalse(self.repeated(p,r))
        self.assertTrue(compiler.validate_compiled(r,p))  # Other validation is not bypassed.

    def test_same_scene_locked_identity_allowed(self):
        p,r=self.case(); p['shots'][1]['scene_number']=1
        self.assertFalse(self.repeated(p,r))

    def test_unknown_scene_still_rejected(self):
        p,r=self.case(); p['shots'][1]['scene_number']=None
        self.assertTrue(self.repeated(p,r))

    def test_different_vault_id_still_rejected(self):
        p,r=self.case(); p['shots'][1]['character_references'][0]['character_id']='different-meera'
        self.assertTrue(self.repeated(p,r))

    def test_missing_provenance_still_rejected(self):
        p,r=self.case(); p['shots'][0]['character_references'][0].pop('locked_vault_description')
        self.assertTrue(self.repeated(p,r))

    def test_changed_description_still_rejected(self):
        p,r=self.case(); p['shots'][1]['character_references'][0]['locked_vault_description']='A different outfit.'
        self.assertTrue(self.repeated(p,r))

    def test_real_generic_style_rejection_remains(self):
        p,r=self.case('restrained natural rendering carrying fine grain across the')
        self.assertTrue(self.repeated(p,r))

    def test_sliding_window_at_identity_action_boundary(self):
        p,r=self.case('indian documentary director in her early thirties sits')
        self.assertFalse(self.repeated(p,r))
        p,r=self.case('indian documentary director in her early thirties sits beside the window watching golden light settle across the empty room')
        self.assertTrue(self.repeated(p,r))

    def test_locked_appearance_phrase_allowed(self):
        p,r=self.case('her deep teal linen jacket and cream trousers')
        self.assertFalse(self.repeated(p,r))
        p,r=self.case('deep teal linen jacket cream trousers and a')
        self.assertFalse(self.repeated(p,r))

    def test_real_name_prefix_and_shortened_wardrobe_allowed(self):
        for phrase in ['meera the poised indian documentary director in her']:
            p,r=self.case(phrase)
            self.assertFalse(self.repeated(p,r))
        p,r=self.case('black hair teal linen jacket and small silver')
        self.assertTrue(self.repeated(p,r))

    def test_reordered_or_invented_attributes_rejected(self):
        for phrase in ['cream jacket black hair teal linen trousers silver',
                       'black hair teal linen jacket and golden pendant']:
            p,r=self.case(phrase)
            self.assertTrue(self.repeated(p,r))

    def test_locked_identity_may_recur_when_returning_to_a_scene(self):
        p,r=self.case()
        p['shots'].append({**copy.deepcopy(p['shots'][0]),'shot_number':3})
        r['shots'].append({'shot_number':3,'compiled_prompt':PHRASE+'.'})
        self.assertFalse(self.repeated(p,r))

    def test_generic_hardware_explanation_still_rejected(self):
        p,r=self.case('Arri Alexa tonal latitude')
        self.assertTrue(any('repeated hardware phrasing' in e for e in compiler.validate_compiled(r,p)))

    def test_invented_description_never_becomes_locked(self):
        result=source('Seedance 2.0')
        result['continuity']['characters']=[{'name':'Meera','description':LOCKED,'character_id':'untrusted'}]
        result['shots'][0]['characters_in_shot']=['Meera']
        ref=compiler.compiler_input(result,emit=Mock())['shots'][0]['character_references'][0]
        self.assertNotIn('locked_vault_description',ref)


if __name__ == '__main__': unittest.main()
