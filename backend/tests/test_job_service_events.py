import unittest
from unittest.mock import Mock

from app.services import job_service


class JobServiceEventTests(unittest.TestCase):
    def test_append_event_commits_without_reloading_the_same_row(self):
        db = Mock()
        event = job_service.append_event(db, 'job-1', 'qa', 'Reviewing')
        db.add.assert_called_once_with(event)
        db.commit.assert_called_once_with()
        db.refresh.assert_not_called()

    def test_append_events_uses_one_transaction_for_a_diagnostic_burst(self):
        db = Mock()
        rows = job_service.append_events(db, 'job-1', [('preview_timing', 'one'),
                                                        ('still_frame', 'two')])
        self.assertEqual([(row.agent_key, row.note) for row in rows],
                         [('preview_timing', 'one'), ('still_frame', 'two')])
        db.add_all.assert_called_once_with(rows)
        db.commit.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
