"""Deterministic boundary tests; live provider evidence is kept in the audit."""
import copy
import base64
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import VoiceRateProfile
from app.services import voice_timing as timing, voice_generation_service as voice, job_service
from app.agents import director, prompts
from test_voice_generation import pcm


class TimingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_profiles_install_once_and_keep_recalibration(self):
        timing.ensure_measured_profiles(self.db)
        self.assertEqual(self.db.query(VoiceRateProfile).count(), 407)
        row = self.db.get(VoiceRateProfile, ("priya", "Hindi"))
        row.chars_per_second = 23
        self.db.commit()
        timing.ensure_measured_profiles(self.db)
        self.assertEqual(self.db.query(VoiceRateProfile).count(), 407)
        self.assertEqual(row.chars_per_second, 23)

    def test_selection_uses_all_shots_once_and_preserves_vault(self):
        result = {"continuity": {"characters": [
            {"name": "Asha", "description": "adult woman", "gender": "female", "age_bracket": "adult", "voice_assignment": "heuristic"},
            {"name": "Vault", "description": "adult woman", "voice_assignment": "vault", "character_id": "fixed", "voice_id": "priya"},
        ]}, "shots": [
            {"has_dialogue": True, "characters_in_shot": ["Asha"], "dialogue_text": "अ" * 60, "duration_sec": 4},
            {"has_dialogue": True, "characters_in_shot": ["Asha"], "dialogue_text": "आ" * 100, "duration_sec": 8},
        ]}
        records = timing.select_calibrated_voices(self.db, result, "Hindi")
        candidates = records[0]["candidates"]
        self.assertEqual({c["voice_id"] for c in candidates}, voice.FEMALE_VOICE_IDS)
        for candidate in candidates:
            rate = candidate["chars_per_second"]
            self.assertAlmostEqual(candidate["mean_pace_deviation"], (abs(60/rate/4-1)+abs(100/rate/8-1))/2)
        self.assertEqual(records[0]["selected_voice_id"], min(candidates, key=lambda c: c["mean_pace_deviation"])["voice_id"])
        self.assertEqual(result["continuity"]["characters"][1]["voice_id"], "priya")
        self.assertEqual(timing.select_calibrated_voices(self.db, result, "Hindi"), [])

    def test_native_script_limit_mood_and_decoded_duration(self):
        timing.native_script_guard("आपका order तैयार है।", "Hindi")
        for text in ["Aapka order taiyar hai", "अ"*2501]:
            with self.assertRaises(voice.VoiceGenerationError): timing.native_script_guard(text, "Hindi")
        self.assertEqual(timing.mood_pace("urgent excitement"), 1.12)
        self.assertEqual(timing.mood_pace("solemn, tender"), .88)
        self.assertEqual(timing.mood_pace("neutral"), 1)
        self.assertEqual(timing.mood_pace("उत्साहित"), 1.12)
        self.assertEqual(timing.mood_pace("कोमल"), .88)
        self.assertEqual(timing.eligible_voices({"gender": "female", "age_bracket": "older_adult"}), ["roopa"])
        self.assertEqual(timing.eligible_voices({"gender": "male", "age_bracket": "teen"}), ["kabir"])
        self.assertEqual(voice.decoded_audio_duration(pcm(2.375)), 2.375)

    def test_casting_uses_structured_fields_independent_of_description_language(self):
        hindi = "युवती, नीली कुर्ती, घर पर सुबह चाय बनाती, सौम्य भाव"
        english = "Young adult woman in a blue kurta making morning tea at home"
        for description in [hindi, english, "elderly man", ""]:
            character = {"description": description, "gender": "female", "age_bracket": "adult"}
            self.assertEqual(set(timing.eligible_voices(character)), voice.FEMALE_VOICE_IDS)
            self.assertEqual(voice.heuristic_voice_id(character), "priya")
        self.assertEqual(len(timing.eligible_voices({"gender": "male", "age_bracket": "adult"})), 23)
        self.assertEqual(timing.eligible_voices({"gender": "female", "age_bracket": "child"}), ["kavya"])
        self.assertEqual(timing.eligible_voices({"gender": "male", "age_bracket": "older_adult"}), ["ratan"])

    def test_missing_invalid_fields_warn_and_keep_full_pool_fit(self):
        for character in [{"description": "adult woman"}, {"gender": "female"},
                          {"gender": "महिला", "age_bracket": "adult"},
                          {"gender": "female", "age_bracket": "grown-up"},
                          {"gender": ["female"], "age_bracket": "adult"}]:
            self.assertEqual(len(timing.eligible_voices(character)), 37)
            self.assertIn("WITHOUT demographic narrowing", timing.casting_warning(character))
            self.assertEqual(voice.heuristic_voice_id(character), "shubh")
        # A deliberate unspecified classification is distinguishable from missing metadata.
        self.assertEqual(len(timing.eligible_voices({"gender": "unspecified", "age_bracket": "adult"})), 37)
        self.assertEqual(timing.eligible_voices({"gender": "nonbinary", "age_bracket": "teen"}), ["kavya", "kabir"])

    def test_vault_and_already_selected_legacy_voices_skip_classification(self):
        characters = [
            {"name": "Vault", "character_id": "approved-id", "voice_assignment": "vault", "voice_id": "priya"},
            {"name": "Legacy", "voice_assignment": "calibrated", "voice_id": "shruti"},
        ]
        result = {"continuity": {"characters": characters}, "shots": [
            {"has_dialogue": True, "characters_in_shot": ["Vault", "Legacy"], "duration_sec": 5, "dialogue_text": "नमस्ते"}]}
        before = copy.deepcopy(characters)
        self.assertEqual(timing.select_calibrated_voices(self.db, result, "Hindi"), [])
        self.assertEqual(characters, before)
        self.assertEqual(voice.assign_missing_voice_ids(result["continuity"]), 0)
        self.assertEqual([c["voice_id"] for c in characters], ["priya", "shruti"])

    def test_new_character_missing_classification_completes_fit_with_trace_warning(self):
        events = []
        character = {"name": "New character", "description": "युवती"}
        result = {"continuity": {"characters": [character]}, "shots": [
            {"has_dialogue": True, "characters_in_shot": ["New character"], "dialogue_text": "अ"*57, "duration_sec": 5}]}
        emit = lambda key, note: events.append((key, note))
        voice.assign_missing_voice_ids(result["continuity"], emit=emit)
        self.assertEqual(character["voice_assignment"], "heuristic")
        record = timing.select_calibrated_voices(self.db, result, "Hindi", emit=emit)[0]
        self.assertEqual(len(record["candidates"]), 37)
        self.assertFalse(record["classification_valid"])
        self.assertEqual(character["voice_assignment"], "calibrated")
        self.assertEqual(record["selected_voice_id"], min(record["candidates"], key=lambda c:c["mean_pace_deviation"])["voice_id"])
        self.assertEqual([key for key, _ in events], ["casting_warning", "casting_warning"])
        self.assertIn("all 37 voices", character["casting_warning"])

    def test_audio_trim_cannot_delete_or_retime_measured_dialogue(self):
        original = [{"shot_number": 1, "duration_sec": 7, "has_dialogue": True,
                     "dialogue_text": "Keep this.", "dialogue_audio_duration_sec": 7, "dialogue_audio_url": "stored.wav"}]
        notes = []
        with patch.object(director, "call_agent", side_effect=[{"total_duration_sec": 1}, {"shots": []}, {"total_duration_sec": 2}]):
            result = director.assemble_shots(original, [], 3, emit=lambda key, note: notes.append(note))
        self.assertEqual(result["shots"], original)
        self.assertEqual(result["assembly"]["total_duration_sec"], 7)
        self.assertTrue(any("trim rejected" in note for note in notes))


class CorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_payload_only_uses_supported_v3_controls(self):
        client = Mock()
        response = Mock()
        response.json.return_value = {"request_id": "fixture", "audios": [base64.b64encode(pcm()).decode()]}
        client.post = AsyncMock(return_value=response)
        with patch.object(voice, "settings", sarvam_api_key="fixture"):
            audio = await voice._sarvam_tts(client, "नमस्ते", "priya", "Hindi", pace=.88)
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["pace"], .88)
        self.assertEqual(payload["model"], "bulbul:v3")
        self.assertTrue({"temperature", "pitch", "loudness", "ssml"}.isdisjoint(payload))
        self.assertEqual(voice.decoded_audio_duration(audio.data), 5)

    async def run_shot(self, measured, retry=None, mood="neutral", target=5):
        updates, events = [], []
        audio = lambda seconds: voice.GeneratedAudio(pcm(seconds), "audio/wav", ".wav", "sarvam")
        retry_mock = AsyncMock(side_effect=retry if isinstance(retry, Exception) else None,
                               return_value=audio(retry if isinstance(retry, (float, int)) else 5))
        with (
            patch.object(voice, "synthesize_dialogue", new=AsyncMock(return_value=audio(measured))) as first,
            patch.object(voice, "_sarvam_tts", retry_mock),
            patch.object(job_service, "update_shot_fields", side_effect=lambda *args, **kwargs: updates.append(kwargs)),
            patch.object(job_service, "append_shot_status_event"),
            patch.object(job_service, "append_event", side_effect=lambda *args: events.append(args[-1])),
            patch.object(voice.storage_service, "upload_bytes", return_value={"url": "saved.wav"}),
        ):
            await voice._generate_dialogue_shot(Mock(), "audit", {"shot_number": 1, "duration_sec": target,
                "dialogue_text": "A test line", "voice_refs": {"Asha": "priya"}}, "English", Mock(), mood)
        return updates[-1], retry_mock, first, events

    async def test_within_tolerance_keeps_target_without_retry(self):
        result, retry, first, _ = await self.run_shot(5.45, mood="excited")
        self.assertEqual(result["duration_sec"], 5)
        self.assertEqual(first.call_args.kwargs["pace"], 1.12)
        retry.assert_not_awaited()

    async def test_near_mismatch_has_only_one_retry_even_when_still_wrong(self):
        result, retry, _, events = await self.run_shot(5.6, retry=6)
        retry.assert_awaited_once()
        self.assertAlmostEqual(retry.call_args.kwargs["pace"], 1.12)
        self.assertEqual(result["duration_sec"], 6)
        self.assertEqual(len(result["dialogue_timing"]["attempts"]), 2)
        self.assertTrue(any("corrected" in event for event in events))

    async def test_large_mismatch_adjusts_duration_without_retry(self):
        result, retry, _, _ = await self.run_shot(7)
        retry.assert_not_awaited()
        self.assertEqual(result["duration_sec"], 7)

    async def test_retry_failure_retains_first_audio(self):
        result, retry, _, _ = await self.run_shot(5.6, retry=RuntimeError("provider unavailable"))
        retry.assert_awaited_once()
        self.assertEqual(result["duration_sec"], 5.6)
        self.assertEqual(result["status"], "done")

    async def test_preflight_blocks_both_providers(self):
        with patch.object(voice, "_sarvam_tts", new=AsyncMock()) as sarvam, patch.object(voice, "_elevenlabs_tts", new=AsyncMock()) as eleven:
            with self.assertRaises(voice.VoiceGenerationError):
                await voice.synthesize_dialogue(Mock(), text="x"*2501, voice_id="shubh", language="English")
        sarvam.assert_not_awaited()
        eleven.assert_not_awaited()

    async def test_final_assembly_receives_persisted_corrected_audio(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            job = job_service.create_job(db, "timing test")
            result = {"format": {"duration_target_sec": 10}, "continuity": {"characters": []},
                      "shots": [{"shot_number": 1, "duration_sec": 1, "has_dialogue": True,
                                 "dialogue_text": "Keep this.", "voice_refs": {"Narrator": "shubh"}, "status": "pending"}]}
            job_service.set_result(db, job.id, result)
            seen = []
            def assemble(system, user, **kwargs):
                self.assertEqual(system, prompts.SHOT_ASSEMBLER)
                seen.extend(json.loads(user))
                return {"total_duration_sec": 4, "transitions": []}
            def compile_after_assembly(result, **kwargs):
                self.assertEqual(result["assembly"]["total_duration_sec"], 4)
                self.assertFalse(result["assembly"]["provisional"])
                self.assertEqual(result["shots"][0]["dialogue_audio_duration_sec"], 4)
                return [{**s, "compiled_prompt": "Audited compiler boundary"} for s in result["shots"]]
            with patch.object(voice, "synthesize_dialogue", new=AsyncMock(return_value=voice.GeneratedAudio(pcm(4), "audio/wav", ".wav", "sarvam"))), patch.object(voice.storage_service, "upload_bytes", return_value={"url": "saved.wav"}), patch.object(director, "call_agent", side_effect=assemble), patch.object(director, "compile_shot_prompts", side_effect=compile_after_assembly) as compiler:
                await voice.generate_job_dialogue_audio(db, job.id)
            compiler.assert_called_once()
            self.assertEqual(seen[0]["duration_sec"], 4)
            self.assertEqual(seen[0]["dialogue_audio_duration_sec"], 4)
            self.assertEqual(seen[0]["status"], "done")
            final = job_service.job_result(job_service.get_job(db, job.id))
            self.assertFalse(final["audio_assembly_pending"])
            self.assertFalse(final["assembly"]["provisional"])
        engine.dispose()
