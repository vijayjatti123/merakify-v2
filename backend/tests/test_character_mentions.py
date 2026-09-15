import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Character, Job


class CharacterMentionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([
            Character(id="vault-one", name="Meera", display_name="Meera", catalog_status="customer", description="Locked identity", image_url="https://example.test/meera.png", image_source="uploaded", status="approved", voice_id="neha"),
            Character(id="vault-two", name="Meera", display_name="Meera", catalog_status="customer", description="Different person", image_url="https://example.test/other.png", image_source="uploaded", status="approved", voice_id="ritu"),
            Character(id="draft", name="Draft", description="Draft", image_url="https://example.test/draft.png", image_source="uploaded", status="draft"),
        ])
        self.db.commit()
        app.dependency_overrides[get_db] = lambda: self.db
        self.background = patch("app.routes.jobs._run_in_background")
        self.background_mock = self.background.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.background.stop()
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def submit(self, brief="Show @Meera beside a lamp.", mentions=None, **extra):
        return self.client.post("/api/jobs", json={"brief": brief, "character_mentions": mentions if mentions is not None else {"@Meera": "vault-one"}, **extra})

    def test_id_persisted_without_switching_to_script_path(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        job = self.db.get(Job, response.json()["id"])
        self.assertIsNone(job.script_text)
        self.assertIn("Show Meera beside a lamp.", job.brief)
        self.assertEqual(json.loads(job.resolutions_json)["characters"]["Meera"], {"mode": "vault", "character_id": "vault-one"})
        self.background_mock.assert_called_once_with(job.id)

    def test_name_collision_resolves_selected_id_not_first_name_match(self):
        response = self.submit(mentions={"@Meera": "vault-two"})
        job = self.db.get(Job, response.json()["id"])
        self.assertEqual(json.loads(job.resolutions_json)["characters"]["Meera"]["character_id"], "vault-two")

    def test_missing_or_draft_id_rejected_before_job_creation(self):
        for identity in ("missing", "draft"):
            self.assertEqual(self.submit(mentions={"@Meera": identity}).status_code, 422)
        self.assertEqual(self.db.query(Job).count(), 0)
        self.background_mock.assert_not_called()

    def test_removed_token_or_prefix_not_bound(self):
        for brief in ("No character here", "Show @MeeraTwo", "mail@Meera", "@@Meera"):
            self.assertEqual(self.submit(brief=brief).status_code, 422)

    def test_invalid_token_rejected(self):
        self.assertEqual(self.submit(mentions={".*": "vault-one"}).status_code, 422)

    def test_unselected_typed_name_and_plain_brief_unchanged(self):
        response = self.submit(mentions={})
        job = self.db.get(Job, response.json()["id"])
        self.assertIn("@Meera", job.brief)
        self.assertIsNone(job.resolutions_json)

    def test_ambiguous_duplicate_names_rejected(self):
        response = self.submit(brief="Show @Meera and @Meera-2", mentions={"@Meera": "vault-one", "@Meera-2": "vault-two"})
        self.assertEqual(response.status_code, 422)
        self.background_mock.assert_not_called()

    def test_existing_script_contract_remains(self):
        response = self.submit(brief="A supplied script", mentions={}, script_text="Meera enters.", resolutions={"characters": {"Meera": {"mode": "vault", "character_id": "vault-one"}}, "locations": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.get(Job, response.json()["id"]).script_text, "Meera enters.")

    def test_existing_upload_voice_approve_routes_produce_usable_mention(self):
        # External storage and paid reference-sheet generation are test doubles;
        # the upload, voice, approval, list and intake routes/persistence are real.
        with patch("app.routes.character_routes.storage_service.upload_file", return_value={"key": "audit.png", "url": "https://example.test/upload.png"}):
            response = self.client.post("/api/characters/upload", data={"name": "Inline person", "display_name": "Inline person", "catalog_status": "customer", "description": "A teal jacket"}, files={"file": ("audit.png", b"image fixture", "image/png")})
        self.assertEqual(response.status_code, 200)
        identity = response.json()["id"]
        self.assertEqual(response.json()["status"], "draft")
        self.assertEqual(self.client.post(f"/api/characters/{identity}/voice", json={"voice_id": "neha"}).status_code, 200)
        with patch("app.routes.character_routes.character_image_service.generate_character_reference_sheet", side_effect=RuntimeError("controlled sheet outage")):
            approved = self.client.post(f"/api/characters/{identity}/approve")
        self.assertEqual(approved.json()["status"], "approved")
        self.assertEqual(approved.json()["reference_sheet_error"], "controlled sheet outage")
        result = self.submit(brief="Show @Inline_person", mentions={"@Inline_person": identity})
        self.assertEqual(result.status_code, 200)
        job = self.db.get(Job, result.json()["id"])
        self.assertEqual(json.loads(job.resolutions_json)["characters"]["Inline person"]["character_id"], identity)

    def test_customer_list_excludes_approved_test_unreviewed_and_unnamed_records(self):
        self.db.add_all([
            Character(id="fixture", name="Friendly name", display_name="Friendly", catalog_status="test", description="fixture", image_url="x", image_source="uploaded", status="approved"),
            Character(id="legacy", name="Ordinary name", description="unreviewed", image_url="x", image_source="uploaded", status="approved"),
            Character(id="unnamed", name="Internal batch", catalog_status="customer", description="no display name", image_url="x", image_source="uploaded", status="approved"),
        ])
        self.db.commit()
        with patch("app.routes.character_routes.storage_service.refresh_asset_url", side_effect=lambda url: url):
            response = self.client.get("/api/characters")
        self.assertEqual({c["id"] for c in response.json()}, {"vault-one", "vault-two"})
        for identity in ("fixture", "legacy", "unnamed"):
            self.assertEqual(self.submit(mentions={"@Meera": identity}).status_code, 422)

    def test_clean_display_name_is_used_in_brief_and_resolution_not_batch_name(self):
        c = self.db.get(Character, "vault-one")
        c.name = "INTERNAL Batch Meera 2026-09-11"
        self.db.commit()
        response = self.submit()
        job = self.db.get(Job, response.json()["id"])
        self.assertNotIn("INTERNAL", job.brief)
        self.assertEqual(json.loads(job.resolutions_json)["characters"]["Meera"]["character_id"], c.id)
        self.assertEqual(c.name, "INTERNAL Batch Meera 2026-09-11")

    def test_customer_creation_requires_explicit_nonblank_display_name_before_upload(self):
        with patch("app.routes.character_routes.storage_service.upload_file") as upload:
            for display in ("", "   "):
                response = self.client.post("/api/characters/upload", data={"name": "batch", "description": "desc", "catalog_status": "customer", "display_name": display}, files={"file": ("image.png", b"image", "image/png")})
                self.assertEqual(response.status_code, 422)
            upload.assert_not_called()

    def test_presentation_update_preserves_internal_identity(self):
        response = self.client.patch("/api/characters/vault-one/presentation", json={"display_name": "Meera", "catalog_status": "test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "vault-one")
        self.assertEqual(response.json()["name"], "Meera")
        self.assertEqual(self.submit().status_code, 422)

    def test_legacy_migration_is_conservative_and_idempotent(self):
        from sqlalchemy import text
        from app import db as database
        legacy_engine = create_engine("sqlite://")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE characters (id TEXT PRIMARY KEY, name TEXT, status TEXT)")
            for identity in [*database.REVIEWED_CHARACTER_CATALOG, "unknown"]:
                connection.execute(text("INSERT INTO characters VALUES (:id, 'unchanged internal label', 'approved')"), {"id": identity})
        with patch.object(database, "engine", legacy_engine):
            database.ensure_character_reference_sheet_column()
            with legacy_engine.begin() as connection:
                rows = {r["id"]: dict(r) for r in connection.execute(text("SELECT * FROM characters")).mappings()}
                self.assertEqual(rows["0eacb44a-3b89-4598-b89d-492073a16051"]["display_name"], "Meera")
                self.assertEqual(rows["e0e41b06-539b-40a0-b9e1-f8b73cf2aee9"]["display_name"], "Tara")
                self.assertEqual(rows["53e9e215-e9a6-4213-ac2e-1e0c25d8c292"]["catalog_status"], "test")
                self.assertEqual(rows["unknown"]["catalog_status"], "review_required")
                self.assertIsNone(rows["unknown"]["display_name"])
                connection.execute(text("UPDATE characters SET catalog_status='test' WHERE display_name='Meera'"))
            database.ensure_character_reference_sheet_column()
            with legacy_engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT catalog_status FROM characters WHERE display_name='Meera'")).scalar(), "test")
        legacy_engine.dispose()


if __name__ == "__main__":
    unittest.main()
