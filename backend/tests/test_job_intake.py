import unittest

from pydantic import ValidationError

from app.agents import prompts
from app.schemas import JobCreate


class JobIntakeTests(unittest.TestCase):
    def test_defaults_create_a_complete_job_request(self) -> None:
        payload = JobCreate(brief="A one-line idea")

        self.assertEqual(payload.aspect_ratio, "16:9")
        self.assertEqual(payload.quality, "720p")
        self.assertEqual(payload.language, "English")
        self.assertEqual(payload.ai_model, "Seedance 2.5")

    def test_non_english_rejects_models_without_audio_reference_support(self) -> None:
        with self.assertRaises(ValidationError):
            JobCreate(brief="एक विज्ञापन", language="Hindi", ai_model="Veo 3.1")

    def test_non_english_accepts_seedance_models(self) -> None:
        payload = JobCreate(brief="एक विज्ञापन", language="Hindi", ai_model="Seedance 2.0")

        self.assertEqual(payload.ai_model, "Seedance 2.0")

    def test_script_architect_prompt_receives_selected_language(self) -> None:
        system_prompt = prompts.SCRIPT_ARCHITECT % "Hindi"

        self.assertIn("selected dialogue language is Hindi", system_prompt)
        self.assertIn("native script", system_prompt)


if __name__ == "__main__":
    unittest.main()
