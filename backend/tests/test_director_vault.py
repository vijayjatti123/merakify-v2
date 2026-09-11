import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.director import _apply_continuity_overrides


class ApprovedVaultContinuityTests(unittest.TestCase):
    @patch("app.agents.director.character_service.list_approved_characters")
    def test_case_insensitive_match_uses_vault_fields_selectively(self, list_approved) -> None:
        list_approved.return_value = [
            SimpleNamespace(
                id="ravi-1",
                name="Ravi",
                description="Vault-approved production coordinator",
                image_url="https://example.test/ravi.png",
                voice_id="rahul",
            )
        ]
        unmatched = {
            "name": "Meera",
            "description": "AI-invented jeweller in a linen kurta",
            "voice_sample_ref": None,
        }
        continuity = {
            "characters": [
                {
                    "name": "rAvI",
                    "description": "AI-invented description that must be replaced",
                    "voice_sample_ref": None,
                },
                unmatched,
            ]
        }

        stats = _apply_continuity_overrides(object(), continuity)

        self.assertEqual(stats["automatic_characters"], 1)
        self.assertEqual(
            continuity["characters"][0],
            {
                "name": "rAvI",
                "description": "Vault-approved production coordinator",
                "voice_assignment": "vault",
                "character_id": "ravi-1",
                "image_url": "https://example.test/ravi.png",
                "voice_id": "rahul",
                "voice_sample_ref": "rahul",
            },
        )
        self.assertIs(continuity["characters"][1], unmatched)
        self.assertEqual(
            continuity["characters"][1],
            {
                "name": "Meera",
                "description": "AI-invented jeweller in a linen kurta",
                "voice_sample_ref": None,
            },
        )
        list_approved.assert_called_once()

    @patch("app.agents.director.character_service.list_approved_characters")
    def test_no_match_leaves_continuity_output_unchanged(self, list_approved) -> None:
        list_approved.return_value = [
            SimpleNamespace(
                id="ravi-1",
                name="Ravi",
                description="Vault description",
                image_url="https://example.test/ravi.png",
                voice_id="rahul",
            )
        ]
        continuity = {
            "characters": [
                {
                    "name": "Asha",
                    "description": "AI-invented botanist in a green apron",
                    "voice_sample_ref": None,
                }
            ],
            "locations": [{"name": "Greenhouse", "description": "Warm glasshouse at dawn"}],
            "props": [{"name": "Seed packet"}],
            "narrator_voice_ref": None,
        }
        original_character = continuity["characters"][0]
        original = {
            "characters": [dict(original_character)],
            "locations": [dict(continuity["locations"][0])],
            "props": [dict(continuity["props"][0])],
            "narrator_voice_ref": None,
        }

        stats = _apply_continuity_overrides(object(), continuity)

        self.assertEqual(stats["automatic_characters"], 0)
        self.assertEqual(continuity, original)
        self.assertIs(continuity["characters"][0], original_character)
        list_approved.assert_called_once()


if __name__ == "__main__":
    unittest.main()
