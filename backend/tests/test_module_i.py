import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as database
from app.db import Base, get_db
from app.main import app
from app.services import job_service


class StyleIntakeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        def dependency():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = dependency
        self.background = patch('app.routes.jobs._run_in_background')
        self.background.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.background.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_typed_style_survives_create_get_retry_and_overrides_legacy_lines(self):
        response = self.client.post('/api/jobs', json={
            'brief': 'A tea ad.\n\nVisual style: Realistic.\n\nWhole-video color grade: Cool.',
            'visual_style': 'Cartoon / Anime', 'color_grade': 'Warm',
        })
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual(job['visual_style'], 'Cartoon / Anime')
        self.assertEqual(job['color_grade'], 'Warm')
        self.assertNotIn('Visual style: Realistic.', job['brief'])
        self.assertIn('Visual style: Cartoon / Anime.', job['brief'])
        self.assertNotIn('Whole-video color grade: Cool.', job['brief'])
        retried = self.client.post('/api/jobs/'+job['id']+'/retry').json()
        fetched = self.client.get('/api/jobs/'+retried['id']).json()
        self.assertEqual((fetched['visual_style'], fetched['color_grade']), ('Cartoon / Anime', 'Warm'))

    def test_explicit_defaults_override_legacy_and_omitted_defaults_keep_simple_brief(self):
        simple = self.client.post('/api/jobs', json={'brief': 'A single sentence.'}).json()
        self.assertEqual(simple['brief'], 'A single sentence.')
        self.assertEqual((simple['visual_style'], simple['color_grade']), ('Natural', 'None'))
        reset = self.client.post('/api/jobs', json={'brief': 'Ad\nVisual style: Cinematic.', 'visual_style': 'Natural'}).json()
        self.assertEqual(reset['brief'], 'Ad')
        legacy = self.client.post('/api/jobs', json={'brief': 'Ad\nVisual style: Cinematic.'}).json()
        self.assertEqual(legacy['visual_style'], 'Cinematic')

    def test_invalid_enums_are_rejected(self):
        for field, value in [('visual_style', 'Oil paint'), ('color_grade', 'Cinematic')]:
            response = self.client.post('/api/jobs', json={'brief': 'Ad', field: value})
            self.assertEqual(response.status_code, 422)

    def test_script_text_is_never_rewritten_by_style_mirror(self):
        source = 'INT. STUDIO - DAY\nVisual style: Realistic.\nMira: Keep this exact line.'
        response = self.client.post('/api/jobs', json={'brief': source, 'script_text': source,
            'resolutions': {'characters': {}, 'locations': {}}, 'visual_style': 'Cartoon / Anime'}).json()
        with self.sessions() as db:
            self.assertEqual(job_service.get_job(db, response['id']).script_text, source)


class StyleMigrationTests(unittest.TestCase):
    def test_old_jobs_backfill_once_and_keep_explicit_updates(self):
        engine = create_engine('sqlite://')
        with engine.begin() as c:
            c.execute(text('CREATE TABLE jobs (id VARCHAR PRIMARY KEY, brief TEXT)'))
            c.execute(text('INSERT INTO jobs VALUES (:id,:brief)'), {'id':'old','brief':'Ad\nVisual style: Cartoon / Anime.\nWhole-video color grade: Warm.'})
        with patch.object(database, 'engine', engine):
            database.ensure_job_intake_columns()
            with engine.begin() as c:
                self.assertEqual(c.execute(text('SELECT visual_style,color_grade FROM jobs')).one(), ('Cartoon / Anime','Warm'))
                c.execute(text("UPDATE jobs SET visual_style='Natural'"))
            database.ensure_job_intake_columns()
            with engine.connect() as c:
                self.assertEqual(c.execute(text('SELECT visual_style FROM jobs')).scalar(), 'Natural')
        engine.dispose()
