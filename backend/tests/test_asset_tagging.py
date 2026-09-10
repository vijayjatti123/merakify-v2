import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.routes.jobs import assets_router


class AssetTaggingTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        app = FastAPI()
        app.include_router(assets_router)

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.db.close()

    @patch("app.routes.jobs.storage_service.asset_url")
    @patch("app.routes.jobs.storage_service.upload_file")
    def test_upload_with_role_and_label_persists_in_library(self, upload_file, asset_url) -> None:
        upload_file.return_value = {
            "key": "assets/tagged/storefront.png",
            "url": "https://example.test/tagged/storefront.png",
        }
        asset_url.return_value = "https://example.test/tagged/storefront.png"

        uploaded = self.client.post(
            "/api/assets/upload",
            data={"role": "location", "label": "the shop's storefront"},
            files={"file": ("storefront.png", b"image", "image/png")},
        )

        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["role"], "location")
        self.assertEqual(uploaded.json()["label"], "the shop's storefront")

        listing = self.client.get("/api/assets")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["role"], "location")
        self.assertEqual(listing.json()[0]["label"], "the shop's storefront")

    @patch("app.routes.jobs.storage_service.upload_file")
    def test_upload_without_tags_remains_backward_compatible(self, upload_file) -> None:
        upload_file.return_value = {
            "key": "assets/untagged/reference.png",
            "url": "https://example.test/untagged/reference.png",
        }

        uploaded = self.client.post(
            "/api/assets/upload",
            files={"file": ("reference.png", b"image", "image/png")},
        )

        self.assertEqual(uploaded.status_code, 200)
        self.assertIsNone(uploaded.json()["role"])
        self.assertIsNone(uploaded.json()["label"])

    @patch("app.routes.jobs.storage_service.upload_file")
    def test_upload_rejects_unknown_role(self, upload_file) -> None:
        response = self.client.post(
            "/api/assets/upload",
            data={"role": "character"},
            files={"file": ("reference.png", b"image", "image/png")},
        )

        self.assertEqual(response.status_code, 422)
        upload_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
