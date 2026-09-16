import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agents import llm_client


def response(stop, text):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=None, id="audit", _request_id="request")


class CinematographyRecoveryTests(unittest.TestCase):
    def test_budget_scales_and_is_bounded(self):
        budget = llm_client.cinematography_token_budget
        self.assertEqual(budget([{}], 9), 8192)
        self.assertGreater(budget([{}] * 6, 30), budget([{}] * 4, 30))
        self.assertGreater(budget([{"dialogue_or_vo": "हिन्दी " * 1000}], 9), budget([{}], 9))
        self.assertEqual(budget([{}] * 1000, 10000), 16384)

    def test_only_truncation_retries_with_identical_inputs(self):
        client = Mock()
        client.messages.create.return_value = response("max_tokens", '{"shots":[')
        client.with_options.return_value.messages.create.return_value = response("end_turn", '{"shots":[{"shot_number":1}]}')
        observed = []
        with patch.object(llm_client, "_client", client):
            result = llm_client.call_agent("rules", "job", max_tokens=8192,
                truncation_retry_tokens=16384, on_response=observed.append)
        self.assertEqual(len(result["shots"]), 1)
        first = client.messages.create.call_args.kwargs
        second = client.with_options.return_value.messages.create.call_args.kwargs
        self.assertEqual(second, {**first, "max_tokens": 16384})
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual([r["will_retry"] for r in observed], [True, False])

    def test_two_truncations_fail_without_partial_output_or_third_call(self):
        client = Mock()
        client.messages.create.return_value = response("max_tokens", "PRIVATE LINE")
        client.with_options.return_value.messages.create.return_value = response("max_tokens", "PRIVATE LINE")
        with patch.object(llm_client, "_client", client), self.assertRaisesRegex(ValueError, "one recovery attempt") as caught:
            llm_client.call_agent("rules", "job", max_tokens=8192, truncation_retry_tokens=16384)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertEqual(client.with_options.return_value.messages.create.call_count, 1)

    def test_success_malformed_and_transport_failure_do_not_trigger_recovery(self):
        for value in [response("end_turn", '{"ok":true}'), response("end_turn", '{broken}'), RuntimeError("transport")]:
            client = Mock()
            if isinstance(value, Exception):
                client.messages.create.side_effect = value
            else:
                client.messages.create.return_value = value
            with patch.object(llm_client, "_client", client):
                if value is not None and not isinstance(value, Exception) and value.content[0].text == '{"ok":true}':
                    self.assertEqual(llm_client.call_agent("rules", "job", max_tokens=8192,
                        truncation_retry_tokens=16384), {"ok": True})
                else:
                    with self.assertRaises(RuntimeError if isinstance(value, Exception) else ValueError):
                        llm_client.call_agent("rules", "job", max_tokens=8192, truncation_retry_tokens=16384)
            client.with_options.assert_not_called()

    def test_retry_requires_explicit_opt_in(self):
        client = Mock()
        client.messages.create.return_value = response("max_tokens", '{"shots":[')
        with patch.object(llm_client, "_client", client), self.assertRaisesRegex(ValueError, "cut off"):
            llm_client.call_agent("other agent", "job", max_tokens=4096)
        client.messages.create.assert_called_once()
        client.with_options.assert_not_called()


if __name__ == "__main__":
    unittest.main()
