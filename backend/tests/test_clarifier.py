import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services import clarifier_service as service, job_service as storage


class ClarifierTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_confidence_stops_without_question(self):
        with patch.object(service, "call_agent", return_value={"topic": None, "confidence": .8}):
            row = service.start(self.db, "A complete brief", {})
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.turns, [])

    def test_known_creative_topics_are_not_reasked_even_on_failure(self):
        with patch.object(service, "call_agent", side_effect=RuntimeError("outage")):
            row = service.start(self.db, "Lamp", {"tone": "quiet", "constraints": "no people"})
            self.assertEqual(row.turns[0]["topic"], "differentiator")
            row = service.act(self.db, row, "answer", "Adjustable arm")
        self.assertEqual(len(row.turns), 1)
        self.assertEqual(row.status, "degraded")

    def test_invalid_model_topic_uses_visible_fallback(self):
        with patch.object(service, "call_agent", return_value={"topic": "language", "confidence": .3}):
            row = service.start(self.db, "Lamp", {"language": "Tamil"})
        self.assertEqual(row.status, "degraded")
        self.assertEqual(row.turns[0]["topic"], "tone")
        self.assertTrue(row.turns[0]["warning"])

    def test_atomic_stale_writer_cannot_overwrite(self):
        row = storage.create_clarifier_session(self.db, "Lamp", {})
        session_id, revision = row.session_id, row.revision
        with self.sessions() as other:
            stale = storage.get_clarifier_session(other, session_id)
            self.assertEqual(stale.revision, revision)
            storage.update_clarifier_session(self.db, session_id, revision, {"status": "cancelled"})
            with self.assertRaises(ValueError):
                storage.update_clarifier_session(other, session_id, revision, {"raw_brief": "overwrite"})
        actual = storage.get_clarifier_session(self.db, session_id)
        self.assertEqual(actual.raw_brief, "Lamp")
        self.assertEqual(actual.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
