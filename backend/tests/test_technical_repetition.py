import unittest
from app.services import shot_prompt_compiler as compiler
from test_vault_repetition import VaultRepetitionTests


class TechnicalRepetitionTests(unittest.TestCase):
    def case(self, text, lens="85mm shallow depth"):
        payload, response = VaultRepetitionTests().case(text)
        for shot in payload["shots"]:
            shot["lens"] = lens
            shot["hardware_language"] = "Arri Alexa tonal latitude"
        return payload, response

    def repetition(self, text, lens="85mm shallow depth"):
        payload, response = self.case(text, lens)
        return [e for e in compiler.validate_compiled(response, payload) if "repeated" in e]

    def test_fixed_fact_and_boundary_grammar(self):
        self.assertFalse(self.repetition("eighty-five-millimeter shallow-depth lens, the Arri Alexa keeps"))

    def test_numeric_notation(self):
        self.assertFalse(self.repetition("85mm shallow-depth lens, the Arri Alexa keeps"))

    def test_ungrounded_focal_length_or_depth_is_not_exempt(self):
        text = "eighty-five-millimeter shallow-depth lens preserves exceptional bright background contours"
        for lens in ("", "normal lens", "50mm shallow depth", "85mm deep focus"):
            self.assertTrue(self.repetition(text, lens), lens)

    def test_unlocked_prose_on_both_sides_is_still_checked(self):
        generic = "the curtain moves gently beside the open window every morning"
        fact = "eighty-five-millimeter shallow-depth lens"
        for text in (generic + ". " + fact, fact + ". " + generic):
            self.assertTrue(self.repetition(text))

    def test_hardware_explanation_still_checked(self):
        self.assertTrue(any("repeated hardware phrasing" in e for e in
                            self.repetition("Arri Alexa tonal latitude preserves the existing highlights")))

    def test_fact_exemption_does_not_skip_other_validation(self):
        payload, response = self.case("eighty-five-millimeter shallow-depth lens, the Arri Alexa keeps")
        errors = compiler.validate_compiled(response, payload)
        self.assertTrue(any("word count" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
