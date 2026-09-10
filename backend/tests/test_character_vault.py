import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.routes.character_routes import router as characters_router
from app.services.character_image_service import GeneratedCharacterImage


class CharacterVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        app = FastAPI()
        app.include_router(characters_router)

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.db.close()

    @patch("app.routes.character_routes.storage_service.upload_bytes")
    @patch("app.routes.character_routes.character_image_service.generate_character_image")
    def test_generate_retry_replaces_same_draft_with_a_new_url(self, generate_image, upload_bytes) -> None:
        generate_image.return_value = GeneratedCharacterImage(b"image", "image/png")
        upload_bytes.side_effect = [
            {"key": "characters/first.png", "url": "https://example.test/first.png"},
            {"key": "characters/second.png", "url": "https://example.test/second.png"},
        ]

        first = self.client.post(
            "/api/characters/generate",
            json={"name": "Meera", "description": "An architect in a linen jacket"},
        )
        self.assertEqual(first.status_code, 200)

        retried = self.client.post(
            "/api/characters/generate",
            json={
                "name": "Meera",
                "description": "An architect in a linen jacket",
                "character_id": first.json()["id"],
            },
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["id"], first.json()["id"])
        self.assertNotEqual(retried.json()["image_url"], first.json()["image_url"])
        self.assertEqual(retried.json()["image_source"], "generated")

    @patch("app.routes.character_routes.storage_service.upload_file")
    def test_upload_creates_an_independent_draft(self, upload_file) -> None:
        upload_file.return_value = {
            "key": "characters/upload/reference.png",
            "url": "https://example.test/uploaded.png",
        }
        response = self.client.post(
            "/api/characters/upload",
            data={"name": "Arjun", "description": "A chef in a white apron"},
            files={"file": ("reference.png", b"image", "image/png")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["image_source"], "uploaded")
        self.assertEqual(response.json()["status"], "draft")

    @patch("app.routes.character_routes.storage_service.upload_bytes")
    @patch("app.routes.character_routes.character_image_service.generate_character_image")
    def test_voice_approval_and_approved_only_listing(self, generate_image, upload_bytes) -> None:
        generate_image.return_value = GeneratedCharacterImage(b"image", "image/png")
        upload_bytes.return_value = {
            "key": "characters/meera.png",
            "url": "https://example.test/meera.png",
        }
        draft = self.client.post(
            "/api/characters/generate",
            json={"name": "Meera", "description": "An architect in a linen jacket"},
        ).json()

        self.assertEqual(self.client.get("/api/characters").json(), [])

        voiced = self.client.post(
            f"/api/characters/{draft['id']}/voice",
            json={"voice_id": "priya"},
        )
        self.assertEqual(voiced.status_code, 200)
        self.assertEqual(voiced.json()["voice_id"], "priya")

        approved = self.client.post(f"/api/characters/{draft['id']}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], "approved")
        self.assertEqual(approved.json()["voice_id"], "priya")

        listing = self.client.get("/api/characters")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([character["id"] for character in listing.json()], [draft["id"]])


if __name__ == "__main__":
    unittest.main()
