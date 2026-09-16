import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import PromptTechnique
from app.services import prompt_technique_service as knowledge
from app.services.shot_prompt_compiler import word_range


class TechniqueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.patch = patch.object(knowledge, "SessionLocal", self.session)
        self.patch.start()
        knowledge.seed_techniques()

    def tearDown(self):
        self.patch.stop()
        self.engine.dispose()

    def ids(self, content, model):
        with self.session() as db:
            return {r["id"] for r in knowledge.lookup_techniques(db, content, model)}

    def test_union_not_intersection_or_null_wildcard(self):
        common = {"x-identity-reinforcement"}
        self.assertEqual(self.ids("ad", "Kling 3.0"), common | {"x-kling-word-count"})
        self.assertEqual(self.ids("ad", "Seedance 2.5"), common)
        self.assertEqual(self.ids("dialogue", "hedra"), common | {"x-hedra-expression"})
        self.assertEqual(self.ids("dialogue", "seedance"), common | {"x-hedra-expression"})
        self.assertEqual(self.ids("ad", "hedra"), common | {"x-hedra-expression"})

    def test_seed_does_not_duplicate_or_overwrite(self):
        with self.session() as db:
            db.get(PromptTechnique, "x-kling-word-count").guidance_text = "Curated update"
            db.commit()
        knowledge.seed_techniques()
        with self.session() as db:
            self.assertEqual(db.query(PromptTechnique).count(), 5)
            self.assertEqual(db.get(PromptTechnique, "x-kling-word-count").guidance_text, "Curated update")

    def test_once_per_shot_and_batch_scoping(self):
        payload = {"content_type": "ad", "ai_model": "Kling 3.0", "shots": [
            {"shot_number": 1, "has_dialogue": False}, {"shot_number": 2, "has_dialogue": True, "speech_mode": "onscreen"}]}
        with patch.object(knowledge, "lookup_techniques", wraps=knowledge.lookup_techniques) as lookup:
            found = knowledge.shot_knowledge(payload, lambda *args: None)
            self.assertEqual(lookup.call_count, 2)
        silent = knowledge.knowledge_addendum(payload["shots"][:1], found)
        dialogue = knowledge.knowledge_addendum(payload["shots"][1:], found)
        self.assertIn("x-kling-word-count", silent)
        self.assertNotIn("x-hedra-expression", silent)
        self.assertIn("x-hedra-expression", dialogue)
        self.assertNotIn("x-kling-word-count", dialogue)

    def test_other_word_ranges_unchanged(self):
        self.assertEqual(word_range({"model_family": "kling"}, {"has_dialogue": False}), (50, 100))
        for family in ("generic", "veo", "sora", "kling"):
            self.assertEqual(word_range({"model_family": family}, {"has_dialogue": True}), (100, 150))
        for family in ("generic", "veo", "sora"):
            self.assertEqual(word_range({"model_family": family}, {}), (100, 150))
