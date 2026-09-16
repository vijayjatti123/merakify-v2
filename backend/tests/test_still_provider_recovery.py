import base64
import io
import json
import unittest
from unittest.mock import patch
from PIL import Image
from app.services import still_frame_service as service


class StillProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        raw = io.BytesIO()
        Image.new("RGB", (160, 90)).save(raw, "PNG")
        self.good = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw.getvalue()).decode()}}]}}]}
        self.empty = {"responseId": "provider-id", "candidates": [{"finishReason": "IMAGE_OTHER",
            "finishMessage": "Miscellaneous generation failure", "safetyRatings": []}], "usageMetadata": {"promptTokenCount": 42}}
        self.result = {"aspect_ratio": "16:9", "shots": [{"shot_number": 1, "compiled_prompt": "A cup on a table."}], "continuity": {}}
        self.events = []

    def run_with(self, responses, verdict=None):
        with patch.object(service, "_google", side_effect=responses) as provider, \
             patch.object(service, "check_still", return_value=verdict or {"approved": True, "reason": "Correct"}) as qa, \
             patch.object(service.storage_service, "upload_bytes", return_value={"url": "accepted", "key": "accepted"}) as upload, \
             patch.object(service.time, "sleep") as delay:
            service.generate_still_frames(self.result, job_id="isolated", emit=lambda k,n: self.events.append((k,n)))
        return provider, qa, upload, delay

    def test_no_image_then_real_candidate_is_checked_and_stored(self):
        provider, qa, upload, delay = self.run_with([self.empty, self.good])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(provider.call_args_list[0], provider.call_args_list[1])
        qa.assert_called_once()
        upload.assert_called_once()
        delay.assert_called_once_with(2)
        self.assertEqual(self.result["shots"][0]["still_frame_url"], "accepted")
        records = [json.loads(n) for k,n in self.events if k == "still_provider_response"]
        self.assertEqual([r["attempt"] for r in records], [1, 2])
        self.assertEqual(records[0]["candidates"][0]["finishMessage"], "Miscellaneous generation failure")
        self.assertEqual(records[0]["responseId"], "provider-id")
        self.assertNotIn("inlineData", json.dumps(records))

    def test_two_empty_responses_stop_without_qa_or_storage(self):
        provider, qa, upload, _ = self.run_with([self.empty, self.empty])
        self.assertEqual(provider.call_count, 2)
        qa.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(self.result["shots"][0]["still_frame_status"], "failed")

    def test_qa_failure_does_not_stack_another_generation_after_no_image_retry(self):
        provider, qa, upload, _ = self.run_with([self.empty, self.good], {"approved": False, "reason": "Wrong framing"})
        self.assertEqual(provider.call_count, 2)
        qa.assert_called_once()
        upload.assert_not_called()
        self.assertEqual(self.result["shots"][0]["still_frame_status"], "failed")

    def test_safety_and_unknown_responses_are_not_retried(self):
        responses = [
            {"candidates": [{"finishReason": "IMAGE_SAFETY"}]},
            {"candidates": [{"finishReason": "IMAGE_PROHIBITED_CONTENT"}]},
            {"candidates": [{"finishReason": "IMAGE_OTHER", "safetyRatings": [{"blocked": True}]}]},
            {"candidates": [{"finishReason": "IMAGE_OTHER"}], "promptFeedback": {"blockReason": "SAFETY"}},
            {"candidates": [{"finishReason": "IMAGE_OTHER"}], "promptFeedback": {"safetyRatings": [{"blocked": True}]}},
            {"candidates": []},
        ]
        for response in responses:
            with self.subTest(response=response):
                provider, qa, upload, delay = self.run_with([response])
                self.assertEqual(provider.call_count, 1)
                qa.assert_not_called()
                upload.assert_not_called()
                delay.assert_not_called()

    def test_diagnostics_omit_echoed_input_and_signed_urls(self):
        response = {"candidates": [{"finishMessage": "private prompt https://bucket/key?X-Amz-Signature=secret"}]}
        record = service._image_response_diagnostics(response, [{"text": "private prompt"}])
        self.assertNotIn("private prompt", json.dumps(record))
        self.assertNotIn("secret", json.dumps(record))
