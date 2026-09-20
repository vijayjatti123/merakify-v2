import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services import clarifier_service as service, job_service as storage


def assessment(missing=(), confidence=.9):
    return {"confidence": confidence, "understanding": "A lamp ad.", "coverage": {
        k: {"status": "missing" if k in missing else "provided", "evidence": "Lamp",
            "question": service.QUESTIONS[k]} for k in service.QUESTIONS}}


class ClarifierTests(unittest.TestCase):
    def test_existing_jobs_receive_optional_direction_column_idempotently(self):
        from sqlalchemy import inspect, text
        from app import db as database
        legacy = create_engine('sqlite://')
        with legacy.begin() as conn:
            conn.execute(text('CREATE TABLE jobs (id VARCHAR PRIMARY KEY, brief TEXT)'))
            conn.execute(text("INSERT INTO jobs VALUES ('old-job','Unchanged source')"))
        with patch.object(database, 'engine', legacy):
            database.ensure_job_intake_columns()
            database.ensure_job_intake_columns()
        self.assertIn('creative_direction_json', {c['name'] for c in inspect(legacy).get_columns('jobs')})
        with legacy.connect() as conn:
            self.assertEqual(tuple(conn.execute(text('SELECT brief, creative_direction_json FROM jobs')).one()), ('Unchanged source', None))
        legacy.dispose()

    def test_evidence_allows_only_ordered_verbatim_elision(self):
        source = "no additional product claims beyond what's stated; keep details open for interpretation."
        self.assertTrue(service._grounded_excerpt("No additional product claims... open for interpretation.", source))
        self.assertFalse(service._grounded_excerpt("No additional product claims... guaranteed strongest bond", source))
        self.assertFalse(service._grounded_excerpt("open for interpretation... no additional product", source))

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_confidence_stops_without_question(self):
        with patch.object(service, "call_agent", return_value=assessment()):
            row = service.start(self.db, "Lamp complete brief", {})
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.turns, [])

    def test_known_creative_topics_are_not_reasked_even_on_failure(self):
        with patch.object(service, "call_agent", side_effect=RuntimeError("outage")):
            row = service.start(self.db, "Lamp", {k: "known" for k in service.QUESTIONS if k != "differentiator"})
            self.assertEqual(row.turns[0]["topic"], "differentiator")
            row = service.act(self.db, row, "answer", "Adjustable arm")
        self.assertEqual(len(row.turns), 1)
        self.assertEqual(row.status, "degraded")

    def test_invalid_model_topic_uses_visible_fallback(self):
        with patch.object(service, "call_agent", return_value={"topic": "language", "confidence": .3}):
            row = service.start(self.db, "Lamp", {"language": "Tamil"})
        self.assertEqual(row.status, "degraded")
        self.assertEqual(row.turns[0]["topic"], "product")
        self.assertTrue(row.turns[0]["warning"])

    def test_high_score_cannot_skip_product_gap(self):
        known = {k: "known" for k in ("duration", "aspect_ratio", "content_type", "color_grade", "visual_style", "quality", "language", "ai_model")}
        with patch.object(service, "call_agent", return_value=assessment(["product"], .99)):
            row = service.start(self.db, "Lamp", known)
        self.assertEqual(row.turns[0]["topic"], "product")
        self.assertLess(row.confidence, .8)
        self.assertNotIn(row.status, ("ready", "refined", "degraded"))

    def test_bounded_questions_and_no_repeated_topics(self):
        def respond(system, *args, **kwargs):
            result = assessment(list(service.QUESTIONS), .99)
            return {"updates": result["coverage"], "confidence": .99} if kwargs.get("fast") else result
        with patch.object(service, "call_agent", side_effect=respond):
            row = service.start(self.db, "Lamp", {})
            for _ in range(service.MAX_QUESTIONS):
                row = service.act(self.db, row, "answer", "Please decide")
        self.assertEqual(len(row.turns), service.MAX_QUESTIONS)
        self.assertEqual(len({t['topic'] for t in row.turns}), service.MAX_QUESTIONS)
        self.assertEqual(row.status, "ready")
        self.assertLess(row.confidence, .8)

    def test_model_cannot_replace_neutral_question_with_claim_rewrite(self):
        result = assessment(["product"], .4)
        result["coverage"]["product"]["question"] = (
            "Should we avoid the confidence claim and frame the drink as refreshment instead?"
        )
        with patch.object(service, "call_agent", return_value=result):
            row = service.start(
                self.db,
                "Kabir drinks Coca-Cola, finds courage, and prepares to jump.",
                {},
                context={"input_mode": "idea", "ad_type": "character", "products": []},
            )
        self.assertEqual(service.MAX_QUESTIONS, 3)
        self.assertEqual(row.turns[0]["topic"], "product")
        self.assertEqual(row.turns[0]["question"], service.QUESTIONS["product"])
        self.assertEqual(row.turns[0]["options"], service.QUESTION_OPTIONS["product"])
        self.assertNotIn("avoid", row.turns[0]["question"].lower())

    def test_existing_pending_model_question_is_neutralized_on_reload_and_answer(self):
        row = storage.create_clarifier_session(self.db, "Kabir drinks Coca-Cola.", {})
        row = storage.update_clarifier_session(self.db, row.session_id, row.revision, {
            "turns": [{"topic": "product", "question": "Should we avoid this claim?",
                       "answer": None, "source": "model", "warning": None}],
            "gathered": {"_assessment": {"coverage": assessment(["product"])["coverage"],
                                           "understanding": "A Coca-Cola ad."}},
        })
        current = service.snapshot(row)
        self.assertEqual(current["turns"][0]["question"], service.QUESTIONS["product"])
        self.assertEqual(current["turns"][0]["options"], service.QUESTION_OPTIONS["product"])
        with patch.object(service, "call_agent", side_effect=RuntimeError("outage")):
            updated = service.act(self.db, row, "answer", "Refreshment before the jump")
        self.assertEqual(updated.turns[0]["question"], service.QUESTIONS["product"])
        self.assertEqual(updated.turns[0]["answer"], "Refreshment before the jump")

    def test_explicit_metaphorical_product_role_does_not_force_product_question(self):
        result = assessment(["audience"], .6)
        result["coverage"]["product"]["evidence"] = "drinks Coca-Cola, finds courage"
        with patch.object(service, "call_agent", return_value=result):
            row = service.start(
                self.db,
                "Kabir drinks Coca-Cola, finds courage, and prepares to jump.",
                {},
                context={"input_mode": "idea", "ad_type": "character", "products": []},
            )
        self.assertEqual(row.turns[0]["topic"], "audience")

    def test_invented_evidence_is_unresolved_without_discarding_valid_assessment(self):
        result = assessment()
        result['coverage']['product']['evidence'] = 'Never supplied benefit'
        with patch.object(service, "call_agent", return_value=result):
            row = service.start(self.db, "Lamp", {})
        self.assertLess(row.confidence, .8)
        self.assertEqual(row.turns[0]['topic'], 'product')
        self.assertEqual(row.turns[0]['source'], 'fallback')
        self.assertEqual(row.gathered['_assessment']['unverified'], ['product'])
        self.assertEqual(row.gathered['_assessment']['coverage']['tone']['status'], 'provided')

    def test_script_handoff_keeps_source_and_rejects_stale_revision(self):
        from app.schemas import JobCreate
        row = storage.create_clarifier_session(self.db, 'Meera: "Hello."', {}, context={'input_mode':'script','products':[]})
        row = storage.update_clarifier_session(self.db, row.session_id, row.revision, {'refined_prompt':'Keep it warm.', 'status':'refined'})
        payload = JobCreate(brief='Meera: "Hello."', script_text='Meera: "Hello."', resolutions={}, clarifier_session_id=row.session_id, clarifier_revision=row.revision)
        handoff = service.accepted_direction(self.db, payload)
        self.assertEqual(handoff['original_input'], 'Meera: "Hello."')
        job = storage.create_job(self.db, payload.brief, creative_direction=handoff)
        self.assertIn('Keep it warm.', job.creative_direction_json)
        payload.clarifier_revision -= 1
        with self.assertRaises(ValueError):
            service.accepted_direction(self.db, payload)

    def test_script_refinement_is_separate_bounded_notes_not_rewritten_script(self):
        row = storage.create_clarifier_session(self.db, 'Meera: "Exact dialogue."', {'language':'Hindi'}, context={'input_mode':'script'})
        notes = {k:'Use the supplied detail.' for k in service.DIRECTION_FIELDS}
        with patch.object(service, 'call_agent', return_value={'production_direction':notes}), patch.object(service, 'lookup_techniques', return_value=[]):
            row = service.act(self.db,row,'refine')
        self.assertEqual(row.raw_brief,'Meera: "Exact dialogue."')
        self.assertNotIn('Exact dialogue.',row.refined_prompt)
        self.assertNotIn('Locked settings',row.refined_prompt)
        self.assertEqual(row.known_fields,{'language':'Hindi'})
        self.assertIn('Visual execution:',row.refined_prompt)

    def test_changed_settings_cannot_reuse_old_review(self):
        from app.schemas import JobCreate
        row = storage.create_clarifier_session(self.db, 'Lamp', {'language':'Hindi'}, context={'input_mode':'idea','products':[]})
        row = storage.update_clarifier_session(self.db,row.session_id,row.revision,{'refined_prompt':'Lamp ad','status':'refined'})
        payload = JobCreate(brief='Lamp ad', language='English',clarifier_session_id=row.session_id,clarifier_revision=row.revision)
        with self.assertRaisesRegex(ValueError,'settings changed'):
            service.accepted_direction(self.db,payload)

    def test_followup_updates_multiple_topics_and_preserves_other_facts(self):
        with patch.object(service, "call_agent", return_value=assessment(["product", "audience", "outcome"])):
            row = service.start(self.db, "Lamp", {})
        delta = {"updates": {k: {"status":"provided", "evidence":"Students; adjustable brightness"}
            for k in ("product", "audience")}, "confidence": .99}
        with patch.object(service, "call_agent", return_value=delta) as provider:
            row = service.act(self.db, row, "answer", "Students; adjustable brightness")
        self.assertTrue(provider.call_args.kwargs["fast"])
        self.assertEqual(provider.call_args.kwargs["request_timeout"], 25)
        self.assertEqual(row.turns[-1]["topic"], "outcome")
        self.assertEqual(row.gathered["_assessment"]["coverage"]["tone"]["evidence"], "Lamp")
        self.assertLess(row.confidence, .8)

    def test_followup_reopens_conflict_and_rejects_invented_evidence(self):
        with patch.object(service, "call_agent", return_value=assessment(["product"])):
            row = service.start(self.db, "Lamp", {})
        delta = {"updates": {"product":{"status":"provided", "evidence":"Invented guarantee"},
            "tone":{"status":"missing", "question":"Should the ad be calm or energetic?"}}, "confidence":.95}
        with patch.object(service, "call_agent", return_value=delta):
            row = service.act(self.db, row, "answer", "Not sure; calm but also energetic")
        self.assertIn("product", row.gathered["_assessment"]["unverified"])
        self.assertIn("tone", row.gathered["_assessment"]["unresolved"])
        self.assertEqual(row.turns[-1]["topic"], "tone")
        self.assertLess(row.confidence, .8)

    def test_invalid_update_is_visibly_degraded(self):
        with patch.object(service, "call_agent", return_value=assessment(["product", "audience"])):
            row = service.start(self.db, "Lamp", {})
        with patch.object(service, "call_agent", return_value={"updates":{}, "confidence":.99}):
            row = service.act(self.db, row, "answer", "Adjustable brightness")
        self.assertEqual(row.status, "degraded")
        self.assertEqual(row.turns[-1]["source"], "fallback")

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
