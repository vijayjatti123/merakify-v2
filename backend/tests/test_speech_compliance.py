import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_render_compliance as base
from app.services import speech_compliance_service as speech, render_compliance_service as gate
from app.services import job_service as jobs, audio_video_service as audio


def finding(status="mismatch"):
    return {"status":status,"confidence":.98,"transcript":"garbled words. Drink this, have confidence.",
            "reason":"Extra speech before the approved line",
            "issues":[{"kind":"extra_speech","start_sec":0,"end_sec":3.7,"evidence":"Audible extra vocal syllables"}] if status=="mismatch" else []}


def raw(v):
    return {"candidates":[{"content":{"parts":[{"text":json.dumps(v)}]}}]}


class SpeechParseTests(unittest.TestCase):
    def test_located_speech_cleanup_keeps_picture_and_earlier_audio(self):
        from app.services.final_assembly_service import ffmpeg
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.mp4'
            made = subprocess.run([ffmpeg(), '-nostdin', '-v', 'error', '-y',
                '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=24:d=5',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=5',
                '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source)],
                capture_output=True, timeout=30)
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors='replace'))
            original = io.BytesIO(source.read_bytes())
            repaired = io.BytesIO(speech.remove_unwanted_speech(original, [
                {'kind': 'extra_speech', 'start_sec': 3, 'end_sec': 4.9,
                 'evidence': 'test tone'}]))
            before, duration = speech.extract_audio(original)
            after, cleaned_duration = speech.extract_audio(repaired)
            self.assertAlmostEqual(duration, cleaned_duration, delta=.1)
            import av
            def picture_frames(media):
                media.seek(0)
                with av.open(media) as movie:
                    return [frame.to_ndarray().tobytes() for frame in movie.decode(video=0)]
            self.assertEqual(picture_frames(original), picture_frames(repaired))
            import wave
            with wave.open(io.BytesIO(before)) as wav:
                first = wav.readframes(24000 * 2)
            with wave.open(io.BytesIO(after)) as wav:
                kept = wav.readframes(24000 * 2)
                wav.readframes(24000)
                muted = wav.readframes(24000)
            from array import array
            def energy(pcm):
                return sum(abs(sample) for sample in array('h', pcm))
            self.assertAlmostEqual(energy(first), energy(kept), delta=energy(first) * .1)
            self.assertLess(energy(muted), energy(kept) // 10)

    def test_silent_audio_accepts_no_words_and_rejects_gibberish(self):
        clear = {"status":"pass", "confidence":.98, "transcript":"",
                 "reason":"Only room tone is audible.", "issues":[]}
        self.assertEqual(speech.parse(raw(clear), 5, expect_silence=True)["status"], "pass")
        self.assertEqual(speech.parse(raw({**finding(), "transcript":""}), 5,
                                      expect_silence=True)["status"], "mismatch")
        with self.assertRaisesRegex(ValueError, "Silent-shot mismatch"):
            speech.parse(raw({**finding(), "issues":[{"kind":"wrong_words", "start_sec":0,
                "end_sec":1, "evidence":"unapproved line"}]}), 5, expect_silence=True)

    def test_equivalent_contractions_normalize_without_hiding_extra_speech(self):
        self.assertEqual(speech.normalized_words("You've got this."),
                         speech.normalized_words("You have got this"))
        self.assertNotEqual(speech.normalized_words("You've got this."),
                            speech.normalized_words("Well, you have got this"))

    def test_checker_contraction_false_positive_is_accepted(self):
        result = finding()
        result.update(transcript='Drink this, Kabir. You have got this.',
                      reason='The contraction was expanded.',
                      issues=[{'kind':'wrong_words','start_sec':1,'end_sec':2,'evidence':'have'}])
        response = io.BytesIO(json.dumps(raw(result)).encode())
        with patch.object(speech, 'urlopen', return_value=response), \
             patch.object(speech.settings, 'google_ai_api_key', 'test'):
            checked = speech.inspect_audio(b'wav', 6, {
                'dialogue_text': "Drink this, Kabir. You've got this.", 'language':'English'})
        self.assertEqual(checked['verdict']['status'], 'pass')
        self.assertEqual(checked['verdict']['issues'], [])

    def test_silent_checker_receives_no_dialogue_contract(self):
        clear = {"status":"pass", "confidence":.98, "transcript":"",
                 "reason":"Only non-vocal ambience is audible.", "issues":[]}
        with patch.object(speech, 'urlopen', return_value=io.BytesIO(json.dumps(raw(clear)).encode())) as call, \
             patch.object(speech.settings, 'google_ai_api_key', 'test'):
            checked = speech.inspect_audio(b'wav', 5, {'mode':'none'})
        self.assertEqual(checked['verdict']['status'], 'pass')
        body = json.loads(call.call_args.args[0].data)
        self.assertIn('speech-like gibberish', body['contents'][0]['parts'][0]['text'])
        self.assertNotIn('Approved transcript context', body['contents'][0]['parts'][0]['text'])

    def test_evidence_required_and_low_confidence_never_retries(self):
        self.assertEqual(speech.parse(raw(finding()),6)["status"],"mismatch")
        v=finding();v["confidence"]=.6
        self.assertEqual(speech.parse(raw(v),6)["status"],"unverified")
        for change in ({"issues":[]},{"confidence":float("nan")},{"issues":[{"kind":"extra_speech","start_sec":-1,"end_sec":3,"evidence":"x"}]}):
            with self.assertRaises(ValueError):speech.parse(raw({**finding(),**change}),6)
        with self.assertRaises(ValueError):speech.parse(raw({**finding(),"status":"pass"}),6)

    def test_clear_timing_mismatch_is_valid_structured_evidence(self):
        verdict = finding()
        verdict.update(reason='Approved line starts before its directed action beat',
            issues=[{'kind':'wrong_timing','start_sec':0,'end_sec':2.1,
                     'evidence':'Expected the line within 4.7-6.9 seconds'}])
        self.assertEqual(speech.parse(raw(verdict), 7)['issues'][0]['kind'], 'wrong_timing')

    def test_strict_prompt_preserves_native_script_exactly(self):
        line="यह जोड़ कभी नहीं टूटेगा।"
        text=audio.approved_speech_text({"language":"Hindi"},{"dialogue_text":line},"Yamaraj")
        self.assertEqual(text.count(line),1)
        for term in ("exactly ONCE","muttering","before and after"):self.assertIn(term,text)


class SpeechGateTests(unittest.TestCase):
    def setUp(self):
        base.ComplianceTests.setUp(self)
        jobs.update_video(self.db,self.job.id,1,video_provider="fal",video_model="minimax/h3-max/reference-to-video",
            video_retry_request={"prompt":"approved direction","reference_image_urls":["scene"],"reference_audio_urls":["approved.wav"]},
            video_compliance_expected={"visual_style":"Natural","speech":{"dialogue_text":"Drink this, have confidence.","language":"English","start_sec":4.7,"end_sec":6.9}})
        self.extract=patch.object(speech,"extract_audio",return_value=(b"wav",6));self.extract.start();self.addCleanup(self.extract.stop)
        self.visual=patch.object(gate,"inspect",side_effect=lambda *a:base.verdict());self.visual.start();self.addCleanup(self.visual.stop)
    tearDown=base.ComplianceTests.tearDown
    data=base.ComplianceTests.data
    check=base.ComplianceTests.check

    def test_mismatch_corrective_retry_once_preserves_references_then_pass_cached(self):
        with patch.object(speech,"inspect_audio",side_effect=[{"verdict":finding()},{"verdict":finding("pass")}]) as checker,patch.object(audio,"submit",return_value={"id":"second"}) as submit:
            self.assertFalse(self.check())
            request=submit.call_args.args[1]
            self.assertEqual(request["reference_audio_urls"],["approved.wav"])
            self.assertEqual(request["reference_image_urls"],["scene"])
            self.assertIn("exactly ONCE",request["prompt"])
            self.assertIn('"start_sec": 4.7',request["prompt"])
            self.assertIn('Generate the voice and visible articulation together',request["prompt"])
            self.assertNotIn("garbled words",request["prompt"])
            self.assertFalse(self.check())
            self.shot["video_task_id"]="second"
            self.assertTrue(self.check());self.assertTrue(self.check())
            submit.assert_called_once();self.assertEqual(checker.call_count,2)
            self.assertEqual(self.data()["video_speech_check"]["status"],"pass")
            self.assertEqual(len(self.data()["video_compliance_checks"]),2)

    def test_second_mismatch_is_visible_without_third_paid_call(self):
        with patch.object(speech,"inspect_audio",side_effect=lambda *a:{"verdict":finding()}),patch.object(audio,"submit",return_value={"id":"second"}) as submit:
            self.assertFalse(self.check());self.shot["video_task_id"]="second"
            self.assertFalse(self.check());self.assertFalse(self.check())
            submit.assert_called_once()
            self.assertEqual(self.data()["video_speech_check"]["status"],"mismatch")
            self.assertEqual(self.data()["video_status"], "review_required")

    def test_outage_no_paid_retry_and_persisted_unverified(self):
        with patch.object(speech,"inspect_audio",side_effect=TimeoutError()),patch.object(audio,"submit") as submit:
            self.assertTrue(self.check());self.assertTrue(self.check());submit.assert_not_called()
            self.assertEqual(self.data()["video_speech_check"]["status"],"unverified")

    def test_visual_outage_does_not_hide_speech_failure(self):
        self.visual.stop()
        with patch.object(gate,"inspect",side_effect=TimeoutError()),patch.object(speech,"inspect_audio",return_value={"verdict":finding()}),patch.object(audio,"submit",return_value={"id":"second"}) as submit:
            self.assertFalse(self.check());submit.assert_called_once()

    def test_uncertain_retry_never_resubmits(self):
        with patch.object(speech,"inspect_audio",return_value={"verdict":finding()}),patch.object(audio,"submit",side_effect=TimeoutError()) as submit:
            self.assertTrue(self.check());self.assertTrue(self.check());submit.assert_called_once()
            self.assertTrue(self.data()["video_retry_submission_unknown"])

    def test_locked_approved_audio_cannot_trigger_paid_speech_rerender(self):
        jobs.update_video(self.db,self.job.id,1,video_audio_lock={
            "policy":"approved-dialogue-plus-silence-v1","approved_audio_duration_sec":2.1})
        with patch.object(speech,"inspect_audio") as checker,patch.object(audio,"submit") as submit:
            self.assertTrue(self.check())
            checker.assert_not_called();submit.assert_not_called()
            saved=self.data()
            self.assertEqual(saved["video_speech_check"]["status"],"pass")
            self.assertEqual(saved["video_speech_check"]["method"],"approved_audio_lock")

    def test_silent_shot_gibberish_never_triggers_paid_audio_correction(self):
        jobs.update_video(self.db, self.job.id, 1, video_compliance_expected={
            "visual_style":"Natural", "speech":{"mode":"none"}})
        self.shot.update(has_dialogue=False, speech_mode="none")
        with patch.object(speech, "inspect_audio", return_value={"verdict":finding()}) as checker, \
             patch.object(speech, "remove_unwanted_speech", side_effect=ValueError("unavailable")), \
             patch.object(audio, "submit", return_value={"id":"second"}) as submit:
            self.assertFalse(self.check())
            self.assertEqual(checker.call_args.args[2], {"mode":"none"})
            submit.assert_not_called()
        saved = self.data()
        self.assertEqual(saved["video_status"], "review_required")
        self.assertIn("unwanted speech", saved["video_error"])
