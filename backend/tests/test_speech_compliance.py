import copy
import io
import json
import unittest
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
    def test_evidence_required_and_low_confidence_never_retries(self):
        self.assertEqual(speech.parse(raw(finding()),6)["status"],"mismatch")
        v=finding();v["confidence"]=.6
        self.assertEqual(speech.parse(raw(v),6)["status"],"unverified")
        for change in ({"issues":[]},{"confidence":float("nan")},{"issues":[{"kind":"extra_speech","start_sec":-1,"end_sec":3,"evidence":"x"}]}):
            with self.assertRaises(ValueError):speech.parse(raw({**finding(),**change}),6)
        with self.assertRaises(ValueError):speech.parse(raw({**finding(),"status":"pass"}),6)

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
            video_compliance_expected={"visual_style":"Natural","speech":{"dialogue_text":"Drink this, have confidence.","language":"English"}})
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
            self.assertTrue(self.check());self.assertTrue(self.check())
            submit.assert_called_once()
            self.assertEqual(self.data()["video_speech_check"]["status"],"mismatch")

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
