import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Character, CharacterStyleVariant
from app.agents.director import _apply_continuity_overrides
from app.services import character_style_service as styles
from app.services.character_image_service import GeneratedCharacterImage


class CharacterStyleVariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'styles.db'}")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.character = Character(name="Audit", description="Green jacket", image_url="https://example.test/base.png", image_source="generated", voice_id="rahul", status="approved")
        self.db.add(self.character)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_natural_does_not_access_provider_or_cache(self):
        with patch.object(styles.character_image_service, "generate_character_reference_sheet") as generate:
            self.assertEqual(styles.resolve_character_rendering(object(), self.character, "Natural"), {"image_url": self.character.image_url})
        generate.assert_not_called()

    def test_generated_variant_is_checked_cached_and_voice_is_unchanged(self):
        with patch.object(styles.character_image_service, "generate_character_reference_sheet", return_value=GeneratedCharacterImage(b"image", "image/png")) as generate, patch.object(styles.character_image_service, "check_character_style_variant", return_value={"approved": True, "reason": "matches"}) as qa, patch.object(styles.storage_service, "upload_bytes", return_value={"key": "variant", "url": "https://example.test/variant.png"}):
            first = styles.resolve_character_rendering(self.db, self.character, "Cartoon / Anime")
            second = styles.resolve_character_rendering(self.db, self.character, "Cartoon / Anime")
        self.assertEqual(first, second)
        self.assertEqual(first["style_variant_source"], "generated_variant")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(qa.call_count, 1)
        self.db.refresh(self.character)
        self.assertEqual(self.character.voice_id, "rahul")
        self.assertEqual(self.character.image_url, "https://example.test/base.png")
        self.assertEqual(self.db.query(CharacterStyleVariant).count(), 1)

    def test_visual_qa_retries_and_uploaded_source_is_recorded(self):
        self.character.image_source = "uploaded"
        self.db.commit()
        with patch.object(styles.character_image_service, "generate_character_reference_sheet", return_value=GeneratedCharacterImage(b"image", "image/png")) as generate, patch.object(styles.character_image_service, "check_character_style_variant", side_effect=[{"approved": False, "reason": "Restore green jacket"}, {"approved": True, "reason": "matches"}]), patch.object(styles.storage_service, "upload_bytes", return_value={"key": "variant", "url": "https://example.test/variant.png"}):
            result = styles.resolve_character_rendering(self.db, self.character, "3D / CGI")
        self.assertEqual(result["style_variant_source"], "style_transfer_from_upload")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args.kwargs["qa_feedback"], "Restore green jacket")

    def test_failure_falls_back_without_cache_and_emits_warning(self):
        events = []
        with patch.object(styles.character_image_service, "generate_character_reference_sheet", side_effect=RuntimeError("private provider detail")):
            result = styles.resolve_character_rendering(self.db, self.character, "Cinematic", lambda *args: events.append(args))
        self.assertEqual(result["image_url"], self.character.image_url)
        self.assertIn("Visual style mismatch fallback", result["style_variant_warning"])
        self.assertNotIn("private provider detail", str(events))
        self.assertEqual(self.db.query(CharacterStyleVariant).count(), 0)

    def test_two_qa_rejections_never_upload_or_cache(self):
        with patch.object(styles.character_image_service, "generate_character_reference_sheet", return_value=GeneratedCharacterImage(b"image", "image/png")) as generate, patch.object(styles.character_image_service, "check_character_style_variant", return_value={"approved": False, "reason": "wrong identity"}), patch.object(styles.storage_service, "upload_bytes") as upload:
            result = styles.resolve_character_rendering(self.db, self.character, "Realistic")
        self.assertEqual(generate.call_count, 2)
        upload.assert_not_called()
        self.assertIn("style_variant_warning", result)

    def test_unique_pair_enforced_by_database(self):
        for _ in range(2):
            self.db.add(CharacterStyleVariant(character_id=self.character.id, visual_style="Cinematic", image_url="variant", source="generated_variant"))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_override_handles_auto_explicit_missing_and_invent_without_voice_changes(self):
        rendering = {"image_url": "variant", "style_variant_id": "cached", "style_variant_source": "generated_variant"}
        for mode in ("automatic", "explicit", "missing", "invent"):
            continuity = {"characters": [] if mode == "missing" else [{"name": "Audit", "description": "proposal"}]}
            resolutions = {} if mode == "automatic" else {"characters": {"Audit": {"mode": "invent" if mode == "invent" else "vault", "character_id": self.character.id}}}
            original = copy.deepcopy(continuity)
            with patch.object(styles, "resolve_character_rendering", return_value=rendering) as resolve:
                _apply_continuity_overrides(self.db, continuity, resolutions, job_brief="Brief\n\nVisual style: Cartoon / Anime.")
            if mode == "invent":
                self.assertEqual(original, continuity)
                resolve.assert_not_called()
            else:
                self.assertEqual(continuity["characters"][0]["image_url"], "variant")
                self.assertEqual(continuity["characters"][0]["voice_id"], "rahul")
                self.assertEqual(continuity["characters"][0]["voice_sample_ref"], "rahul")
                self.assertEqual(resolve.call_args.args[2], "Cartoon / Anime")

    def test_brief_parser_uses_exact_dropdown_values_only(self):
        for style in styles.VISUAL_STYLES:
            self.assertEqual(styles.visual_style_from_brief(f"Brief\n\nVisual style: {style}."), style)
        self.assertEqual(styles.visual_style_from_brief("A Cinematic film about Natural products"), "Natural")
        self.assertEqual(styles.visual_style_from_brief("Visual style: Cartoon / Anime.\n\nVisual style: Natural."), "Natural")


if __name__ == "__main__":
    unittest.main()
