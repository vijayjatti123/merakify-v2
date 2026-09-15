import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services import voice_generation_service as voice
from app.services import video_generation_service as video
from tests.test_voice_generation import pcm


class DialogueDurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_decoded_audio_keeps_timing_correction_without_obsolete_warning(self):
        shot = dict(shot_number=1, duration_sec=9, has_dialogue=True,
                    dialogue_text="A complete long speaking turn.", voice_refs={"Speaker": "priya"})
        generated = voice.GeneratedAudio(pcm(12), "audio/wav", ".wav", "sarvam")
        with (patch.object(voice, "synthesize_dialogue", AsyncMock(return_value=generated)),
              patch.object(voice.storage_service, "upload_bytes", return_value={"url": "https://example.com/audio.wav"}),
              patch.object(voice.job_service, "update_shot_fields") as update,
              patch.object(voice.job_service, "append_shot_status_event"),
              patch.object(voice.job_service, "append_event") as event):
            await voice._generate_dialogue_shot(Mock(), "audit", shot, "English", Mock(), "neutral")
        final = update.call_args.kwargs
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["duration_sec"], 12)
        self.assertEqual(final["dialogue_audio_duration_sec"], 12)
        notes = " ".join(call.args[-1] for call in event.call_args_list)
        self.assertIn("duration_sec corrected", notes)
        self.assertNotIn("9-second video-model limit", notes)


class SilentDurationTests(unittest.TestCase):
    def test_seedance_duration_rounding_and_bounds_remain(self):
        result = dict(ai_model="Seedance 2.0", quality="720p", continuity={})
        shot = dict(shot_number=1, compiled_prompt="A still cup on the table.",
                    still_frame_url="https://example.com/still.jpg", has_dialogue=False,
                    characters_in_shot=[])
        for planned, sent in [(1, 4), (4, 4), (4.2, 5), (9, 9), (15, 15)]:
            shot["duration_sec"] = planned
            self.assertEqual(video.translate(result, shot)["request"]["duration"], sent)
        for planned in [0, -1, 15.1, float("nan"), float("inf")]:
            shot["duration_sec"] = planned
            with self.assertRaises(ValueError):
                video.translate(result, shot)
