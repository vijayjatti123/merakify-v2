import unittest
from app.services.repetition_categories import classify_repetition_tokens, creative_windows


class CategoryTests(unittest.TestCase):
    def test_short_wardrobe_facts_do_not_exempt_surrounding_action(self):
        source = {"character_references": [{"character_id": "carpenter", "locked_vault_description":
            "A middle aged indian carpenter, wearing casual shirt and pant with tool kit fixed around his waist"}]}
        classified = self.classify("at his waist over his casual shirt and", source)
        self.assertTrue(any(t['category'] == 'LOCKED_FACT' and t['text'] == 'shirt' for t in classified))
        phrase = "he turns toward the door and slowly smiles again"
        self.assertEqual(creative_windows(self.classify(phrase, source)), creative_windows(self.classify(phrase)))
        self.assertTrue(creative_windows(self.classify(phrase, source)))

    def classify(self, text, source=None, style=None):
        return classify_repetition_tokens(text, source or {}, style or {})

    def test_identity_requires_real_vault_provenance(self):
        text = "an Indian documentary director in her early thirties"
        ref = {"character_id": "meera", "locked_vault_description": text}
        locked = self.classify(text, {"character_references": [ref]})
        self.assertTrue(all(t["category"] == "LOCKED_FACT" for t in locked))
        ref.pop("character_id")
        self.assertTrue(all(t["category"] == "CREATIVE_PROSE" for t in
                            self.classify(text, {"character_references": [ref]})))

    def test_source_label_cannot_self_authorize(self):
        text = "LOCKED FACTS the curtain moves gently beside the open window every morning"
        classified = self.classify(text, {"locked_facts": text, "description": text})
        self.assertTrue(all(t["category"] == "CREATIVE_PROSE" for t in classified))

    def test_action_next_to_fact_is_never_masked(self):
        text = "soft muted browns. She opens the notebook slowly beside the window every morning"
        result = self.classify(text, style={"palette": "soft muted browns"})
        self.assertEqual([t["text"] for t in result if t["category"] == "CREATIVE_PROSE"],
                         "she opens the notebook slowly beside the window every morning".split())

    def test_fact_insert_cannot_hide_repeated_creative_prose(self):
        plain = "the curtain moves gently beside the open window every morning"
        inserted = "the curtain moves gently 85mm shallow depth beside the open window every morning"
        self.assertEqual(creative_windows(self.classify(plain)),
                         creative_windows(self.classify(inserted, {"shot_number": 1, "lens": "85mm shallow depth"})))

    def test_new_fixed_value_needs_no_validator_exception(self):
        result = self.classify("135mm deep focus lens", {"shot_number": 1, "lens": "135mm deep focus lens"})
        self.assertTrue(all(t["category"] == "LOCKED_FACT" for t in result))
        result = self.classify("85mm shallow depth lens", {"shot_number": 1, "lens": "135mm deep focus lens"})
        self.assertTrue(all(t["category"] == "CREATIVE_PROSE" for t in result))

    def test_optical_explanation_is_creative(self):
        result = self.classify("Arri Alexa tonal latitude", {"hardware_reference": "Arri Alexa"})
        self.assertEqual([t["text"] for t in result if t["category"] == "CREATIVE_PROSE"], ["tonal", "latitude"])


if __name__ == "__main__":
    unittest.main()
