import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.agents import prompts
from app.agents.director import _apply_continuity_overrides, run_pipeline
from app.schemas import JobCreate


class ScriptResolutionSchemaTests(unittest.TestCase):
    def test_script_text_and_closed_resolution_map_validate_together(self) -> None:
        payload = JobCreate(
            brief="A pasted script\n\nTarget duration: 30 seconds. Content type: Ad.",
            script_text="INT. CAFE - DAY\nRavi enters.",
            resolutions={
                "characters": {"Ravi": {"mode": "vault", "character_id": "character-1"}},
                "locations": {"CAFE": {"mode": "invent"}},
            },
        )

        self.assertEqual(payload.resolutions.characters["Ravi"].character_id, "character-1")
        with self.assertRaises(ValidationError):
            JobCreate(brief="script", script_text="INT. CAFE - DAY")
        with self.assertRaises(ValidationError):
            JobCreate(
                brief="script",
                script_text="INT. CAFE - DAY",
                resolutions={"characters": {"Ravi": {"mode": "invent", "extra": "forbidden"}}},
            )


class ContinuityResolutionTests(unittest.TestCase):
    @patch("app.agents.director.asset_service.list_assets")
    @patch("app.agents.director.character_service.list_approved_characters")
    def test_explicit_resolutions_win_and_invent_stays_ai_generated(self, list_approved, list_assets) -> None:
        list_approved.return_value = [
            SimpleNamespace(
                id="automatic-ravi",
                name="Ravi",
                description="Automatic Ravi",
                image_url="https://example.test/automatic-ravi.png",
                voice_id="rahul",
            ),
            SimpleNamespace(
                id="chosen-character",
                name="Aarohi Reference",
                description="Chosen production reference",
                image_url="https://example.test/chosen.png",
                voice_id="priya",
            ),
            SimpleNamespace(
                id="omar-vault",
                name="Omar",
                description="Vault Omar that explicit invent must suppress",
                image_url="https://example.test/omar.png",
                voice_id="dev",
            ),
        ]
        list_assets.return_value = [
            SimpleNamespace(
                id="location-1",
                filename="cafe.jpg",
                object_key="assets/location-1/cafe.jpg",
                url="https://example.test/cafe.jpg",
                role="location",
                label="The real cafe storefront",
            )
        ]
        invented_omar = {
            "name": "Omar",
            "description": "AI-invented chef in a saffron apron",
            "voice_sample_ref": None,
        }
        continuity = {
            "characters": [
                {"name": "rAvI", "description": "Invented Ravi", "voice_sample_ref": None},
                invented_omar,
            ],
            "locations": [{"name": "LOTUS CAFE", "description": "Invented cafe"}],
        }
        resolutions = {
            "characters": {
                "Ravi": {"mode": "vault", "character_id": "chosen-character"},
                "Omar": {"mode": "invent"},
            },
            "locations": {"Lotus Cafe": {"mode": "asset", "asset_id": "location-1"}},
        }

        with patch(
            "app.agents.director.storage_service.asset_url",
            return_value="https://example.test/fresh-cafe.jpg",
        ) as asset_url:
            stats = _apply_continuity_overrides(object(), continuity, resolutions)

        self.assertEqual(stats, {"automatic_characters": 0, "explicit_characters": 1, "explicit_locations": 1})
        self.assertEqual(continuity["characters"][0]["image_url"], "https://example.test/chosen.png")
        self.assertEqual(continuity["characters"][0]["voice_id"], "priya")
        self.assertIs(continuity["characters"][1], invented_omar)
        self.assertEqual(continuity["locations"][0]["asset_id"], "location-1")
        self.assertEqual(continuity["locations"][0]["image_url"], "https://example.test/fresh-cafe.jpg")
        asset_url.assert_called_once_with("assets/location-1/cafe.jpg")


class ScriptArchitectRoutingTests(unittest.TestCase):
    def _run(self, source_script: str | None):
        resolutions = {
            "characters": {"Ravi": {"mode": "invent"}},
            "locations": {"CAFE": {"mode": "invent"}},
        } if source_script else None
        job = SimpleNamespace(
            id="job-1",
            brief="A source request",
            aspect_ratio="16:9",
            visual_style="Cartoon / Anime",
            color_grade="Warm",
            quality="720p",
            language="English",
            ai_model="Seedance 2.5",
            script_text=source_script,
            resolutions_json=json.dumps(resolutions) if resolutions else None,
        )
        calls = []

        def fake_call(system_prompt, user_prompt, **kwargs):
            calls.append((system_prompt, user_prompt, kwargs))
            if system_prompt == prompts.FORMAT_CLASSIFIER:
                return {"format": "ad", "structure": "three_act", "duration_target_sec": 30, "num_scenes": 2}
            if system_prompt in {prompts.SCRIPT_ARCHITECT_FROM_SCRIPT, prompts.SCRIPT_ARCHITECT % "English"}:
                return {
                    "logline": "Ravi enters the cafe.",
                    "scenes": [{"scene_number": 1, "heading": "CAFE", "description": "Ravi enters.", "dialogue_or_vo": "", "mood": "quiet"}],
                }
            if system_prompt == prompts.CONTINUITY_AGENT:
                return {
                    "characters": [{"name": "Ravi", "description": "AI Ravi", "gender": "male", "age_bracket": "adult", "voice_sample_ref": None}],
                    "locations": [{"name": "CAFE", "description": "AI cafe"}],
                    "props": [],
                    "narrator_voice_ref": None,
                    "visual_style": {"rendering": "cel shading", "palette": "ochre and cream", "lighting_motif": "warm window light", "texture_grain": "ink outlines"},
                }
            if system_prompt == prompts.CINEMATOGRAPHY_AGENT:
                return {
                    "shots": [{
                        "shot_number": 1,
                        "scene_number": 1,
                        "camera_angle": "wide",
                        "camera_movement": "static",
                        "lens": "35mm",
                        "lighting": "soft daylight",
                        "composition_note": "centered",
                        "duration_sec": 5,
                        "description": "Ravi enters",
                        "characters_in_shot": ["Ravi"],
                        "has_dialogue": False,
                        "dialogue_text": "",
                    }]
                }
            if system_prompt == prompts.QA_AGENT:
                return {"approved": True, "issues": []}
            if system_prompt == prompts.SHOT_ASSEMBLER:
                return {"total_duration_sec": 5, "transitions": []}
            raise AssertionError("unexpected prompt")

        with (
            patch("app.agents.director.call_agent", side_effect=fake_call),
            patch("app.services.voice_timing.measured_budget", return_value={"measured_mean_chars_per_second": 18.4}),
            patch("app.agents.director.character_service.list_approved_characters", return_value=[]),
            patch("app.agents.director.job_service.get_job", return_value=job),
            patch("app.agents.director.job_service.set_status"),
            patch("app.agents.director.job_service.append_event"),
            patch("app.agents.director.job_service.set_result") as set_result,
        ):
            run_pipeline(object(), job.id)

        return calls, set_result.call_args.args[2]

    def test_has_script_uses_preservation_prompt_and_returns_source_text(self) -> None:
        source = "INT. CAFE - DAY\nRavi enters."
        calls, result = self._run(source)

        self.assertEqual(calls[1][0], prompts.SCRIPT_ARCHITECT_FROM_SCRIPT)
        self.assertIn(source, calls[1][1])
        self.assertEqual(result["source_script_text"], source)
        continuity_call = next(c for c in calls if c[0] == prompts.CONTINUITY_AGENT)
        self.assertIn('"visual_style": "Cartoon / Anime", "color_grade": "Warm"', continuity_call[1])
        cine_call = next(c for c in calls if c[0] == prompts.CINEMATOGRAPHY_AGENT)
        self.assertIn('"palette": "ochre and cream"', cine_call[1])

    def test_no_script_keeps_original_architect_prompt_and_result_shape(self) -> None:
        calls, result = self._run(None)

        self.assertEqual(calls[1][0], prompts.SCRIPT_ARCHITECT % "English")
        self.assertNotIn("source_script_text", result)


if __name__ == "__main__":
    unittest.main()
