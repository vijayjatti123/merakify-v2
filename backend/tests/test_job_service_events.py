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


if __name__ == '__main__':
    unittest.main()
