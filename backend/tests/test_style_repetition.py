import copy
import unittest
from test_vault_repetition import VaultRepetitionTests

CLAUSE = 'restrained natural rendering carrying fine grain across the'


class StyleRepetitionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = VaultRepetitionTests()
        self.p, self.r = self.fixture.case(CLAUSE)
        self.p['style_bible']['rendering'] = CLAUSE + ' image'

    def errors(self):
        return self.fixture.repeated(self.p, self.r)

    def test_locked_style_across_scenes(self):
        self.assertFalse(self.errors())

    def test_each_global_field(self):
        for field in ('rendering', 'palette', 'texture_grain'):
            self.p['style_bible'] = {field: CLAUSE}
            self.assertFalse(self.errors())

    def test_missing_or_different_source_rejects(self):
        for bible in (None, {}, {'rendering': 'Photorealistic crisp surfaces'}):
            self.p['style_bible'] = bible
            self.assertTrue(self.errors())

    def test_same_or_unknown_scene_rejects(self):
        for scene in (1, None):
            self.p['shots'][1]['scene_number'] = scene
            self.assertTrue(self.errors())

    def test_generic_prose_not_exempted_by_style_label(self):
        for shot in self.r['shots']:
            shot['compiled_prompt'] = 'Render style: the curtain moves gently beside the open window each morning.'
        self.assertTrue(self.errors())

    def test_lighting_and_unrecognized_fields_not_exempted(self):
        for field in ('lighting_motif', 'description', 'notes'):
            self.p['style_bible'] = {field: CLAUSE}
            self.assertTrue(self.errors())

    def test_action_after_style_still_checked(self):
        for shot in self.r['shots']:
            shot['compiled_prompt'] = CLAUSE + ' image. She opens the notebook slowly beside the window every morning.'
        self.assertTrue(self.errors())

    def test_mixed_scene_history(self):
        self.p['shots'].append({**copy.deepcopy(self.p['shots'][0]), 'shot_number': 3})
        self.r['shots'].append({'shot_number': 3, 'compiled_prompt': CLAUSE + '.'})
        self.assertTrue(any('Shot 3' in e for e in self.errors()))

    def test_no_word_bag_or_synonym_matching(self):
        self.p['style_bible'] = {'rendering': 'grain fine carrying natural restrained rendering across the'}
        self.assertTrue(self.errors())

    def test_grounded_sentence_boundary_does_not_swallow_generic_sentence(self):
        for shot in self.r['shots']:
            shot['compiled_prompt'] = 'Before her. Render style: ' + CLAUSE + ' image.'
        self.assertFalse(self.errors())
        for shot in self.r['shots']:
            shot['compiled_prompt'] += ' She opens the notebook slowly beside the window every morning.'
        self.assertTrue(self.errors())

    def test_grounded_sentence_same_scene_still_rejected(self):
        for shot in self.r['shots']:
            shot['compiled_prompt'] = 'Before her. Render style: ' + CLAUSE + ' image.'
        self.p['shots'][1]['scene_number'] = 1
        self.assertTrue(self.errors())
