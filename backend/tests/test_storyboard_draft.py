"""Draft visibility must not bypass the existing approval boundary."""
import unittest
import test_module_f
from app.services import job_service


class StoryboardDraftTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)

    def tearDown(self):
        test_module_f.ModuleFTests.tearDown(self)

    def test_draft_survives_reload_and_error_but_cannot_generate(self):
        draft = {'planning_draft': {'logline': 'A quiet arrival', 'shots': [
            {'shot_number': 1, 'description': 'Ravi enters', 'duration_sec': 5}]}}
        with self.sessions() as db:
            job_service.set_result(db, self.job_id, draft)
            job_service.set_status(db, self.job_id, 'running')
        for status in ('running', 'error'):
            with self.sessions() as db:
                job_service.set_status(db, self.job_id, status)
            for _ in range(2):
                response = self.client.get(f'/api/jobs/{self.job_id}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['result']['planning_draft'], draft['planning_draft'])
                self.assertFalse(response.json()['result'].get('shots'))
            response = self.client.post(f'/api/jobs/{self.job_id}/approve')
            self.assertEqual(response.status_code, 409, response.text)
        with self.sessions() as db:
            job_service.set_result(db, self.job_id, self.original)
            job_service.set_status(db, self.job_id, 'done')
        result = self.client.get(f'/api/jobs/{self.job_id}').json()['result']
        self.assertNotIn('planning_draft', result)
        self.assertEqual(len(result['shots']), 3)
