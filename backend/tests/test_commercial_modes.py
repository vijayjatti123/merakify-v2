import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
from app.schemas import JobCreate
from app.commercial import normalized_brief
from app.services import job_service, clarifier_service
from app.services.preview_plan import preview_input, preview_visual


class CommercialModesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_legacy_intake_and_service_ads_need_no_product(self):
        self.assertEqual(JobCreate(brief="A story").ad_type, "character")
        self.assertEqual(JobCreate(brief="Demonstrate a booking service", ad_type="ugc").ad_type, "ugc")
        for mode in ("product", "cgi"):
            with self.assertRaises(ValueError):
                JobCreate(brief="A reveal", ad_type=mode)

    def test_unknown_mode_and_unbounded_fields_rejected(self):
        for data in ({"ad_type": "other"}, {"ad_brief": {"secret": "x"}}, {"ad_brief": {"audience": "x" * 501}}):
            with self.assertRaises(ValueError):
                JobCreate(brief="A story", **data)

    def test_migration_is_idempotent_and_defaults_existing_jobs(self):
        from app import db as database
        legacy = create_engine("sqlite://")
        with legacy.begin() as connection:
            connection.execute(text("CREATE TABLE jobs (id VARCHAR PRIMARY KEY, brief TEXT)"))
            connection.execute(text("INSERT INTO jobs VALUES ('old', 'Existing story')"))
        with patch.object(database, "engine", legacy):
            database.ensure_job_intake_columns()
            database.ensure_job_intake_columns()
        with legacy.connect() as connection:
            self.assertEqual(tuple(connection.execute(text("SELECT ad_type, ad_brief_json FROM jobs")).one()), ("character", None))
        legacy.dispose()

    def test_real_api_persistence_and_retry_preserve_mode(self):
        from app.main import app
        from app.routes.jobs import get_db
        app.dependency_overrides[get_db] = lambda: self.db
        try:
            # No lifespan worker: actual API + SQLite, no paid planning/generation.
            client = TestClient(app)
            payload = {"brief": "A creator demonstrates a booking service", "ad_type": "ugc",
                       "ad_brief": {"selling_point": "Book in one step", "audio_mode": "voiceover"}}
            response = client.post("/api/jobs", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["id"]
            fetched = client.get(f"/api/jobs/{job_id}").json()
            self.assertEqual(fetched["ad_type"], "ugc")
            self.assertEqual(fetched["ad_brief"]["audio_mode"], "voiceover")
            copied = job_service.copy_job_for_retry(self.db, job_service.get_job(self.db, job_id))
            self.assertEqual(copied.ad_type, "ugc")
            self.assertEqual(json.loads(copied.ad_brief_json), payload["ad_brief"])
            retried = client.post(f"/api/jobs/{job_id}/retry", json={})
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(client.get('/api/jobs/' + retried.json()['id']).json()['ad_type'], 'ugc')
        finally:
            app.dependency_overrides.clear()

    def test_changed_commercial_intake_cannot_reuse_refinement(self):
        row = job_service.create_clarifier_session(self.db, "A service ad", {},
              {"ad_type": "ugc", "ad_brief": {"selling_point": "Simple booking"}})
        row.refined_prompt = "Reviewed service ad"
        self.db.commit()
        base = dict(brief=row.refined_prompt, clarifier_session_id=row.session_id, clarifier_revision=row.revision)
        valid = JobCreate(**base, ad_type="ugc", ad_brief={"selling_point": "Simple booking"})
        self.assertIsNotNone(clarifier_service.accepted_direction(self.db, valid))
        for change in ({"ad_type": "character"}, {"ad_type": "ugc", "ad_brief": {"selling_point": "Different claim"}}):
            with self.assertRaisesRegex(ValueError, "commercial direction changed"):
                clarifier_service.accepted_direction(self.db, JobCreate(**base, **change))
        self.assertEqual(normalized_brief({"audio_mode": "auto", "audience": " "}), {})

    def test_product_retry_keeps_immutable_reference_not_latest_product(self):
        from app.models import Product, JobProduct
        product = Product(name="Audit bottle", original_key="original.png", crop_key="crop.png",
                          accepted_key="approved-v1.png", status="approved")
        self.db.add(product); self.db.commit()
        for mode in ("product", "cgi"):
            original = job_service.create_job(self.db, "Bottle reveal", ad_type=mode, product_ids=[product.id])
            product.accepted_key = "approved-v2.png"
            self.db.commit()
            copied = job_service.copy_job_for_retry(self.db, original)
            expected = self.db.query(JobProduct).filter_by(job_id=original.id).one()
            actual = self.db.query(JobProduct).filter_by(job_id=copied.id).one()
            self.assertEqual(actual.object_key, expected.object_key)
            self.assertEqual(copied.ad_type, mode)

    def test_explicit_audio_treatment_is_checked_without_model_call(self):
        from app.services.director_review import review
        from test_ad_direction import directed
        shot = directed()["shots"][0]
        shot.update(has_dialogue=True, dialogue_text="Approved words", speech_mode="voiceover")
        result = review([shot], [], commercial={"ad_brief": {"audio_mode": "silent"}})
        self.assertTrue(any("selected silent" in i["problem"] for i in result["issues"]))
        result = review([shot], [], commercial={"ad_brief": {"audio_mode": "voiceover"}})
        self.assertFalse(any("treatment" in i["problem"] for i in result["issues"]))

    def test_clarifier_does_not_reask_structured_answers_even_in_fallback(self):
        context = {"ad_type": "ugc", "ad_brief": {"audience": "New parents",
                   "selling_point": "Simple booking", "call_to_action": "Try the app",
                   "treatment": "Phone demo", "must_preserve": "No invented claims"}}
        with patch.object(clarifier_service, "call_agent", side_effect=RuntimeError("offline")):
            row = clarifier_service.start(self.db, "A service ad", {}, context)
            for _ in range(5):
                if not row.turns or row.turns[-1].get("answer") is not None:
                    break
                row = clarifier_service.act(self.db, row, "answer", "Synthetic answer")
        self.assertTrue({t["topic"] for t in row.turns}.isdisjoint(
            {"audience", "differentiator", "outcome", "execution", "constraints"}))
        self.assertNotIn("commercial_guidance", row.gathered["_context"])

    def test_still_context_does_not_leak_future_transformation(self):
        result = {"ad_type": "cgi", "ad_brief": {"treatment": "Bottle explodes into stars"}}
        shot = {"description": "A bottle then explodes into stars", "state_at_shot_start": "An intact bottle on a table",
                "state_at_shot_end": "A field of stars", "characters_in_shot": []}
        visual = preview_visual(preview_input(result, shot))
        self.assertIn("Freeze only", visual)
        self.assertIn("An intact bottle", visual)
        self.assertNotIn("explodes", visual)
        self.assertNotIn("A field of stars", visual)

    def test_prompt_polish_layers_are_deterministic_and_mode_specific(self):
        from app.agents.director import prompt_polish_context
        product = prompt_polish_context("product", brief="a cola bottle ad", language="English",
            duration_seconds=None, aspect_ratio="16:9", visual_style="Natural",
            color_grade="Warm", quality="720p", video_model="h3_max_fal")
        character = prompt_polish_context("character", brief="a runner finds confidence", language="English",
            duration_seconds=None, aspect_ratio="9:16", visual_style="Cinematic",
            color_grade="None", quality="720p", video_model="seedance_mini")
        self.assertIn("PROMPT POLISH CONTRACT", product)
        self.assertIn("PRODUCT PRESET", product)
        self.assertIn('"video_model": "h3_max_fal"', product)
        self.assertIn("CHARACTER PRESET", character)
        self.assertNotIn("PRODUCT PRESET", character)


if __name__ == "__main__":
    unittest.main()


class ProductAlbumContractTests(unittest.TestCase):
    def test_album_limits_and_estimate_are_bounded(self):
        from app.services.product_album_service import options, MODELS
        self.assertEqual(options()["angles"], ["front", "three_quarter", "side", "back", "top", "detail"])
        self.assertGreater(MODELS["pro"][1], MODELS["standard"][1])

    def test_video_reference_angle_selection_is_narrow(self):
        from app.services.product_album_service import relevant_views
        product = {"name": "Bottle", "views": [
            {"id": "side", "angle": "side", "provenance": "original", "object_key": "products/side.png"},
            {"id": "back", "angle": "back", "provenance": "inferred", "object_key": "products/back.png"},
        ]}
        self.assertEqual(relevant_views(product, {"description": "Bottle side view on table", "camera_angle": "side"})[0]["id"], "side")
        self.assertEqual(relevant_views(product, {"description": "Bottle rear label close-up", "camera_angle": "close-up"})[0]["id"], "back")
        self.assertEqual(relevant_views(product, {"description": "A table with no named item"}), [])
