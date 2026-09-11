import asyncio
import io
import wave
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services import voice_generation_service


def pcm(seconds=5):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * int(seconds * 8000))
    return output.getvalue()


class VoiceAssignmentTests(unittest.TestCase):
    def test_assigns_by_age_and_gender_without_overriding_vault_voice(self) -> None:
        continuity = {
            "characters": [
                {"name": "Vault", "description": "young woman", "voice_id": "ritu", "voice_sample_ref": "ritu"},
                {"name": "Mother", "description": "elderly grandmother in a sari", "gender": "female", "age_bracket": "older_adult", "voice_sample_ref": None},
                {"name": "Boy", "description": "young boy carrying a kite", "gender": "male", "age_bracket": "child", "voice_sample_ref": None},
                {"name": "Guide", "description": "quiet masked guide", "gender": "unspecified", "age_bracket": "unspecified", "voice_sample_ref": None},
            ],
            "narrator_voice_ref": None,
        }
        shots = [{"has_dialogue": True,
                "duration_sec": 5, "characters_in_shot": []}]

        assigned = voice_generation_service.assign_missing_voice_ids(continuity, shots)

        self.assertEqual(assigned, 4)
        self.assertEqual(continuity["characters"][0]["voice_id"], "ritu")
        self.assertEqual(continuity["characters"][1]["voice_id"], "roopa")
        self.assertEqual(continuity["characters"][2]["voice_id"], "kabir")
        self.assertEqual(continuity["characters"][3]["voice_id"], "shubh")
        self.assertEqual(continuity["narrator_voice_ref"], "shubh")
        for character in continuity["characters"]:
            self.assertEqual(character["voice_sample_ref"], character["voice_id"])


class ProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sarvam_is_primary_for_english_and_elevenlabs_only_follows_failure(self) -> None:
        fallback_audio = voice_generation_service.GeneratedAudio(b"fallback", "audio/mpeg", ".mp3", "elevenlabs")
        with (
            patch(
                "app.services.voice_generation_service._sarvam_tts",
                new=AsyncMock(side_effect=RuntimeError("rate limited")),
            ) as sarvam,
            patch(
                "app.services.voice_generation_service._elevenlabs_tts",
                new=AsyncMock(return_value=fallback_audio),
            ) as elevenlabs,
        ):
            result = await voice_generation_service.synthesize_dialogue(
                Mock(), text="An English line", voice_id="priya", language="English"
            )

        self.assertEqual(result.provider, "elevenlabs")
        sarvam.assert_awaited_once()
        elevenlabs.assert_awaited_once()

    async def test_both_provider_failure_reasons_include_empty_message_exception_type(self) -> None:
        with (
            patch(
                "app.services.voice_generation_service._sarvam_tts",
                new=AsyncMock(side_effect=RuntimeError("request rejected")),
            ),
            patch(
                "app.services.voice_generation_service._elevenlabs_tts",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            with self.assertRaises(voice_generation_service.VoiceGenerationError) as raised:
                await voice_generation_service.synthesize_dialogue(
                    Mock(), text="A line", voice_id="priya", language="English"
                )

        self.assertEqual(
            str(raised.exception),
            "Sarvam failed (RuntimeError: request rejected); "
            "ElevenLabs fallback failed (TimeoutError)",
        )


class JobVoiceGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dialogue_shots_start_concurrently_and_persist_real_results(self) -> None:
        shots = [
            {
                "shot_number": 1,
                "has_dialogue": True,
                "duration_sec": 5,
                "dialogue_text": "First line",
                "voice_refs": {"Aarohi": "priya"},
            },
            {
                "shot_number": 2,
                "has_dialogue": True,
                "duration_sec": 5,
                "dialogue_text": "Second line",
                "voice_refs": {"Dev": "rahul"},
            },
            {"shot_number": 3, "has_dialogue": False, "dialogue_text": "", "voice_refs": {}},
        ]
        job = SimpleNamespace(language="English")
        both_started = asyncio.Event()
        started = []

        async def synthesize(_client, *, text, voice_id, language, pace):
            started.append((text, voice_id, language))
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            await asyncio.sleep(0.01 if voice_id == "priya" else 0.02)
            return voice_generation_service.GeneratedAudio(
                pcm(), "audio/wav", ".wav", "sarvam"
            )

        update = Mock(return_value={})
        status_event = Mock()
        with (
            patch("app.services.voice_generation_service.job_service.get_job", return_value=job),
            patch("app.services.voice_generation_service.job_service.set_result"),
            patch("app.services.voice_timing.select_calibrated_voices", return_value=[]),
            patch("app.agents.director.finalize_audio_assembly"),
            patch("app.services.voice_generation_service.job_service.job_result", return_value={"shots": shots}),
            patch("app.services.voice_generation_service.job_service.update_shot_fields", update),
            patch("app.services.voice_generation_service.job_service.append_shot_status_event", status_event),
            patch("app.services.voice_generation_service.job_service.append_event"),
            patch("app.services.voice_generation_service.synthesize_dialogue", side_effect=synthesize),
            patch(
                "app.services.voice_generation_service.storage_service.upload_bytes",
                side_effect=lambda key, body, **_kwargs: {"url": f"https://audio.test/{key}"},
            ),
        ):
            await voice_generation_service.generate_job_dialogue_audio(Mock(), "job-1")

        self.assertEqual(
            started,
            [
                ("First line", "priya", "English"),
                ("Second line", "rahul", "English"),
            ],
        )
        done_calls = [call for call in update.call_args_list if call.kwargs.get("status") == "done"]
        self.assertEqual({call.args[2] for call in done_calls}, {1, 2, 3})
        dialogue_done = [call for call in done_calls if call.args[2] in {1, 2}]
        self.assertTrue(all(call.kwargs["dialogue_audio_url"].startswith("https://audio.test/") for call in dialogue_done))
        self.assertTrue(all(call.kwargs["dialogue_audio_provider"] == "sarvam" for call in dialogue_done))

    async def test_one_provider_failure_does_not_cancel_successful_sibling(self) -> None:
        shots = [
            {
                "shot_number": 1,
                "has_dialogue": True,
                "duration_sec": 5,
                "dialogue_text": "This shot fails",
                "voice_refs": {"Aarohi": "priya"},
            },
            {
                "shot_number": 2,
                "has_dialogue": True,
                "duration_sec": 5,
                "dialogue_text": "This shot succeeds",
                "voice_refs": {"Dev": "rahul"},
            },
        ]
        job = SimpleNamespace(language="English")
        both_started = asyncio.Event()
        started = []

        async def synthesize(_client, *, text, voice_id, language, pace):
            started.append((text, voice_id, language))
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            if voice_id == "priya":
                raise voice_generation_service.VoiceGenerationError(
                    "Sarvam failed (HTTP 403); ElevenLabs fallback failed (HTTP 401)"
                )
            await asyncio.sleep(0.01)
            return voice_generation_service.GeneratedAudio(
                pcm(), "audio/wav", ".wav", "sarvam"
            )

        update = Mock(return_value={})
        status_event = Mock()
        with (
            patch("app.services.voice_generation_service.job_service.get_job", return_value=job),
            patch("app.services.voice_generation_service.job_service.set_result"),
            patch("app.services.voice_timing.select_calibrated_voices", return_value=[]),
            patch("app.agents.director.finalize_audio_assembly"),
            patch("app.services.voice_generation_service.job_service.job_result", return_value={"shots": shots}),
            patch("app.services.voice_generation_service.job_service.update_shot_fields", update),
            patch("app.services.voice_generation_service.job_service.append_shot_status_event", status_event),
            patch("app.services.voice_generation_service.job_service.append_event"),
            patch("app.services.voice_generation_service.synthesize_dialogue", side_effect=synthesize),
            patch(
                "app.services.voice_generation_service.storage_service.upload_bytes",
                return_value={"url": "https://audio.test/job-3/shots/2/dialogue.mp3"},
            ),
        ):
            await voice_generation_service.generate_job_dialogue_audio(Mock(), "job-3")

        self.assertEqual(
            started,
            [
                ("This shot fails", "priya", "English"),
                ("This shot succeeds", "rahul", "English"),
            ],
        )
        error_update = next(
            call for call in update.call_args_list
            if call.args[2] == 1 and call.kwargs.get("status") == "error"
        )
        self.assertIn("ElevenLabs fallback failed", error_update.kwargs["error_message"])
        done_update = next(
            call for call in update.call_args_list
            if call.args[2] == 2 and call.kwargs.get("status") == "done"
        )
        self.assertEqual(done_update.kwargs["dialogue_audio_provider"], "sarvam")
        self.assertEqual(
            done_update.kwargs["dialogue_audio_url"],
            "https://audio.test/job-3/shots/2/dialogue.mp3",
        )
        terminal_events = {
            (call.args[2], call.args[3])
            for call in status_event.call_args_list
            if call.args[3] in {"done", "error"}
        }
        self.assertEqual(terminal_events, {(1, "error"), (2, "done")})

    async def test_both_provider_failures_persist_shot_error_and_event(self) -> None:
        shot = {
            "shot_number": 7,
            "has_dialogue": True,
                "duration_sec": 5,
            "dialogue_text": "This call fails",
            "voice_refs": {"Narrator": "shubh"},
        }
        job = SimpleNamespace(language="English")
        update = Mock(return_value={})
        status_event = Mock()
        provider_error = voice_generation_service.VoiceGenerationError(
            "Sarvam failed (HTTP 403); ElevenLabs fallback failed (HTTP 401)"
        )
        with (
            patch("app.services.voice_generation_service.job_service.get_job", return_value=job),
            patch("app.services.voice_generation_service.job_service.set_result"),
            patch("app.services.voice_timing.select_calibrated_voices", return_value=[]),
            patch("app.agents.director.finalize_audio_assembly"),
            patch("app.services.voice_generation_service.job_service.job_result", return_value={"shots": [shot]}),
            patch("app.services.voice_generation_service.job_service.update_shot_fields", update),
            patch("app.services.voice_generation_service.job_service.append_shot_status_event", status_event),
            patch("app.services.voice_generation_service.job_service.append_event"),
            patch("app.services.voice_generation_service.synthesize_dialogue", new=AsyncMock(side_effect=provider_error)),
        ):
            await voice_generation_service.generate_job_dialogue_audio(Mock(), "job-2")

        error_update = next(call for call in update.call_args_list if call.kwargs.get("status") == "error")
        self.assertEqual(error_update.args[2], 7)
        self.assertIn("Sarvam failed", error_update.kwargs["error_message"])
        error_event = next(call for call in status_event.call_args_list if call.args[3] == "error")
        self.assertIn("ElevenLabs fallback failed", error_event.kwargs["error_message"])


if __name__ == "__main__":
    unittest.main()
