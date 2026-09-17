import copy
import unittest
from unittest.mock import patch
from app.agents.director import _attach_voice_refs
from app.agents.dialogue_integrity import protected_dialogue, restore_protected, screen_issues
from app.services import video_generation_service as video, voice_generation_service as voice
from app.services.speech_mode import is_voiceover

class VoiceoverTests(unittest.TestCase):
    def setUp(self):
        self.shot = dict(shot_number=1, scene_number=1, has_dialogue=True, characters_in_shot=[],
            dialogue_text="Morning light reaches the quiet valley.", compiled_prompt="A valley at dawn.",
            dialogue_audio_url="https://example.com/audio.wav", dialogue_audio_duration_sec=5.2,
            still_frame_url="https://example.com/still.png", duration_sec=5)
        self.result = dict(ai_model="Seedance 2.0", quality="720p", continuity={})

    def test_narrator_voice_without_character_and_no_hedra(self):
        continuity = {"characters": []}
        voice.assign_missing_voice_ids(continuity, [self.shot])
        _attach_voice_refs([self.shot], [], continuity["narrator_voice_ref"])
        self.assertEqual(self.shot["voice_refs"], {"Narrator": "shubh"})
        self.assertEqual(voice._shot_voice_id(self.shot), "shubh")
        self.assertFalse(self.shot["experimental_audio_sync"])
        with patch("app.services.hedra_video_service.preview", side_effect=AssertionError("Hedra called")):
            request = video.translate(self.result, self.shot)["request"]
        self.assertEqual(request["model"], video.MODEL)
        self.assertFalse(request["generate_audio"])
        self.assertEqual(request["duration"], 6)

    def test_visible_bystander_is_not_voiceover_speaker(self):
        self.shot.update(speech_mode="voiceover", characters_in_shot=["Worker"])
        _attach_voice_refs([self.shot], [{"name":"Worker", "voice_sample_ref":"priya"}], "shubh")
        self.assertEqual(self.shot["voice_refs"], {"Narrator":"shubh"})

    def test_visible_dialogue_uses_audio_reference_and_silent_stays_seedance(self):
        self.shot.update(characters_in_shot=["Meera"])
        with patch("app.services.hedra_video_service.preview", return_value={"provider":"hedra"}) as h:
            self.assertEqual(video.translate(self.result, self.shot)["provider"], "evolink")
            h.assert_not_called()
        self.shot.update(has_dialogue=False)
        self.assertTrue(video.translate(self.result,self.shot)["request"]["generate_audio"])

    def test_overlong_narration_never_clipped_or_sent_to_hedra(self):
        self.shot["dialogue_audio_duration_sec"] = 16
        with self.assertRaisesRegex(ValueError,"15 seconds"):
            video.translate(self.result,self.shot)

    def test_module_k_protects_unattributed_source_line(self):
        protected=protected_dialogue([self.shot],self.shot["dialogue_text"])
        issues=[{"shot_number":1,"problem":"Visual correction", "fix_instruction":"Delete the dialogue line"}]
        accepted,_,_,limitations=screen_issues([self.shot],issues,protected,lambda *a:None)
        self.assertFalse(accepted); self.assertTrue(limitations)
        revised=copy.deepcopy(self.shot);revised["dialogue_text"]=""
        restored, violations=restore_protected([self.shot],[revised],protected,lambda *a:None)
        self.assertTrue(violations);self.assertEqual(restored[0]["dialogue_text"],self.shot["dialogue_text"])

class NarrationMuxTests(unittest.TestCase):
    def test_real_ffmpeg_mux_preserves_full_audio_and_rejects_short_video(self):
        import tempfile, subprocess, wave
        from pathlib import Path
        from app.services import final_assembly_service as final
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder); video=folder/'source.mp4'; audio=folder/'speech.wav'; out=folder/'out.mp4'
            subprocess.run([final.ffmpeg(),'-y','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=24:d=3',
                '-c:v','libx264',str(video)],check=True)
            subprocess.run([final.ffmpeg(),'-y','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=24000:duration=1.877333333',str(audio)],check=True)
            evidence=final.mux_narration(video,out,audio,1.877333333,lambda x:None)
            self.assertAlmostEqual(evidence['audio_duration'],1.877333333,delta=.05)
            self.assertAlmostEqual(evidence['video_duration'],1.877333333,delta=.05)
            subprocess.run([final.ffmpeg(),'-y','-v','error','-f','lavfi','-i','sine=frequency=440:duration=4',str(audio)],check=True)
            with self.assertRaisesRegex(final.AssemblyError,'shorter'):
                final.mux_narration(video,out,audio,4,lambda x:None)
