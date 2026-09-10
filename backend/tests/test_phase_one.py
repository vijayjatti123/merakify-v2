import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

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
    def test_approve_queues_every_shot_on_the_same_job(self) -> None:
        result = {
            "shots": [
                {"shot_number": 1, "status": "draft"},
                {"shot_number": 2, "status": "draft"},
            ]
        }
        job = completed_job(result)
        db = Mock()

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
            patch("app.routes.jobs.job_service.set_result") as set_result,
        ):
            response = approve_job(job.id, db)

        self.assertEqual(response.id, job.id)
        self.assertTrue(response.result["generation_approved"])
        self.assertEqual([shot["status"] for shot in response.result["shots"]], ["queued", "queued"])
        set_result.assert_called_once_with(db, job.id, response.result)

    def test_regenerate_resets_only_the_requested_shot(self) -> None:
        result = {
            "generation_approved": True,
            "shots": [
                {"shot_number": 1, "status": "ready"},
                {"shot_number": 2, "status": "ready"},
                {"shot_number": 3, "status": "ready"},
            ],
        }
        job = completed_job(result)
        db = Mock()

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
            patch("app.routes.jobs.job_service.set_result") as set_result,
        ):
            response = regenerate_shot(job.id, 2, db)

        self.assertEqual(
            [shot["status"] for shot in response.result["shots"]],
            ["ready", "queued", "ready"],
        )
        set_result.assert_called_once_with(db, job.id, response.result)

    def test_regenerate_requires_approval(self) -> None:
        result = {"generation_approved": False, "shots": [{"shot_number": 1, "status": "draft"}]}
        job = completed_job(result)

        with (
            patch("app.routes.jobs.job_service.get_job", return_value=job),
            patch("app.routes.jobs.job_service.job_result", return_value=result),
        ):
            with self.assertRaises(HTTPException) as raised:
                regenerate_shot(job.id, 1, Mock())

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
