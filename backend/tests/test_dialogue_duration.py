import copy
import unittest
import unicodedata

from app.services.dialogue_duration import estimate_dialogue_duration, preflight_dialogue_durations


class LocalDurationTests(unittest.TestCase):
    def test_inherent_vowels_matras_independent_vowels_and_conjuncts(self):
        for text, count in [("क", 1), ("का", 1), ("क्", 0), ("क्रि", 1), ("आओ", 2), ("कं", 1), ("क़ी", 1)]:
            with self.subTest(text=text):
                self.assertEqual(estimate_dialogue_duration(text)["syllable_units"], count)

    def test_supported_brahmic_scripts_and_normalization(self):
        for text in ["की", "কী", "ਕੀ", "કી", "କୀ", "கீ", "కీ", "ಕೀ", "കീ", "কো", "கொ", "കൊ"]:
            with self.subTest(text=text):
                result = estimate_dialogue_duration(text)
                self.assertEqual(result["syllable_units"], 1)
                self.assertEqual(result["matra_nuclei"], 1)
                self.assertEqual(result, estimate_dialogue_duration(unicodedata.normalize("NFD", text)))
        self.assertEqual(estimate_dialogue_duration("ൻ")["syllable_units"], 0)
        self.assertEqual(estimate_dialogue_duration("ৎ")["syllable_units"], 0)

    def test_punctuation_pause_groups_and_mixed_text(self):
        result = estimate_dialogue_duration("आ, आ... आ?! आ॥")
        self.assertEqual(result["comma_groups"], 1)
        self.assertEqual(result["sentence_end_groups"], 3)
        self.assertEqual(result["pause_sec"], 1.75)
        self.assertEqual(estimate_dialogue_duration("3.14")["sentence_end_groups"], 0)
        self.assertEqual(estimate_dialogue_duration("आपका order 12 तैयार है।")["coverage"], "partial")
        self.assertGreater(estimate_dialogue_duration("Take a look.")["latin_fallback_units"], 0)
        self.assertIsNone(estimate_dialogue_duration("...")["predicted_duration_sec"])

    def test_warning_is_nonblocking_and_does_not_mutate_shots(self):
        shots = [{"shot_number": 1, "has_dialogue": True, "duration_sec": 1,
                  "dialogue_text": "सुबह की चाय तैयार है। आओ, साथ बैठकर आराम से बात करते हैं।"},
                 {"shot_number": 2, "has_dialogue": False, "duration_sec": 2}]
        original = copy.deepcopy(shots)
        events = []
        records = preflight_dialogue_durations(shots, emit=lambda key, note: events.append((key, note)))
        self.assertEqual(shots, original)
        self.assertEqual(len(records), 1)
        self.assertEqual(events[0][0], "duration_preflight_experimental")
        self.assertIn("Experimental duration preflight (low priority)", events[0][1])
        self.assertEqual(records[0]["confidence"], "experimental")
        self.assertEqual(records[0]["priority"], "low")
        self.assertIn("before any dialogue TTS call", events[0][1])
        self.assertGreater(records[0]["relative_divergence"], .25)
        shots[0]["duration_sec"] = records[0]["predicted_duration_sec"]
        self.assertFalse(preflight_dialogue_durations(shots, emit=lambda *_: None)[0]["warning"])

    def test_malformed_timing_is_visible_without_estimator_exception(self):
        for duration in [None, "bad", 0, -1, float("nan"), True]:
            record = preflight_dialogue_durations([{"has_dialogue": True, "duration_sec": duration,
                                                   "dialogue_text": "आओ"}], emit=lambda *_: None)[0]
            self.assertTrue(record["warning"])
            self.assertIsNone(record["relative_divergence"])
