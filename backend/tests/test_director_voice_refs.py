import unittest

from app.agents.director import _attach_voice_refs


class AttachVoiceRefsTests(unittest.TestCase):
    def test_matches_canonically_equivalent_unicode_names(self) -> None:
        characters = [{"name": "Caf\u00e9", "voice_sample_ref": "mother.wav"}]
        shots = [{"characters_in_shot": ["Cafe\u0301"], "has_dialogue": True}]

        result = _attach_voice_refs(shots, characters)

        self.assertEqual(result[0]["voice_refs"], {"Caf\u00e9": "mother.wav"})

    def test_matches_devanagari_candrabindu_and_anusvara_variants(self) -> None:
        characters = [
            {"name": "\u092e\u093e\u0901", "voice_sample_ref": "mother.wav"},
            {"name": "\u092c\u0947\u091f\u0940", "voice_sample_ref": "daughter.wav"},
        ]
        shots = [
            {
                "characters_in_shot": ["\u092e\u093e\u0902", "\u092c\u0947\u091f\u0940"],
                "has_dialogue": True,
            }
        ]

        result = _attach_voice_refs(shots, characters)

        self.assertEqual(
            result[0]["voice_refs"],
            {"\u092e\u093e\u0902": "mother.wav", "\u092c\u0947\u091f\u0940": "daughter.wav"},
        )

    def test_does_not_guess_when_fallback_key_is_ambiguous(self) -> None:
        characters = [
            {"name": "\u092e\u093e\u0901", "voice_sample_ref": "first.wav"},
            {"name": "\u092e\u093e\u0902", "voice_sample_ref": "second.wav"},
        ]
        shots = [{"characters_in_shot": ["\u092e\u093e\u0903"], "has_dialogue": True}]

        result = _attach_voice_refs(shots, characters)

        self.assertEqual(result[0]["voice_refs"], {})


if __name__ == "__main__":
    unittest.main()
