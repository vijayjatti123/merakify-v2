import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.agents.director import (
    SHOT_STATUS_DONE,
    SHOT_STATUS_ERROR,
    SHOT_STATUS_GENERATING,
    SHOT_STATUS_PENDING,
    SHOT_STATUSES,
)
from app.routes.jobs import approve_job, regenerate_shot


def completed_job(result: dict):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id="job-1",
        brief="A test ad",
        aspect_ratio="16:9",
        quality="720p",
        language="English",
        ai_model="Seedance 2.5",
        status="done",
        error_message=None,
        created_at=now,
        updated_at=now,
        result_json="stored",
        result=result,
    )


class PhaseOneRouteTests(unittest.TestCase):
    def test_shot_status_contract_contains_all_four_phase_one_states(self) -> None:
        self.assertEqual(
            SHOT_STATUSES,
            {
                SHOT_STATUS_PENDING,
                SHOT_STATUS_GENERATING,
                SHOT_STATUS_DONE,
                SHOT_STATUS_ERROR,
            },
        )

    def test_approve_marks_every_shot_pending_on_the_same_job(self) -> None:
        result = {
            "shots": [
                {"shot_number": 1, "status": SHOT_STATUS_PENDING},
                {"shot_number": 2, "status": SHOT_STATUS_PENDING},
            ]
        }
        job = completed_job(result)
        db = Mock()
        background_tasks = Mock()

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
            patch("app.routes.jobs.job_service.set_result") as set_result,
        ):
            response = approve_job(job.id, background_tasks, db)

        self.assertEqual(response.id, job.id)
        self.assertTrue(response.result["generation_approved"])
        self.assertEqual(
            [shot["status"] for shot in response.result["shots"]],
            [SHOT_STATUS_PENDING, SHOT_STATUS_PENDING],
        )
        set_result.assert_called_once_with(db, job.id, response.result)
        background_tasks.add_task.assert_called_once()

    def test_regenerate_resets_only_the_requested_shot(self) -> None:
        result = {
            "generation_approved": True,
            "shots": [
                {"shot_number": 1, "status": SHOT_STATUS_DONE},
                {"shot_number": 2, "status": SHOT_STATUS_DONE},
                {"shot_number": 3, "status": SHOT_STATUS_DONE},
            ],
        }
        job = completed_job(result)
        db = Mock()
        background_tasks = Mock()

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
            patch("app.routes.jobs.job_service.set_result") as set_result,
        ):
            response = regenerate_shot(job.id, 2, background_tasks, db)

        self.assertEqual(
            [shot["status"] for shot in response.result["shots"]],
            [SHOT_STATUS_DONE, SHOT_STATUS_PENDING, SHOT_STATUS_DONE],
        )
        set_result.assert_called_once_with(db, job.id, response.result)
        background_tasks.add_task.assert_called_once()

    def test_regenerate_requires_approval(self) -> None:
        result = {
            "generation_approved": False,
            "shots": [{"shot_number": 1, "status": SHOT_STATUS_PENDING}],
        }
        job = completed_job(result)
        background_tasks = Mock()

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
        ):
            with self.assertRaises(HTTPException) as raised:
                regenerate_shot(job.id, 1, background_tasks, Mock())

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
