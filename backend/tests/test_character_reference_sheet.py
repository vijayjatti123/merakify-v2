import base64
import json
import unittest
from unittest.mock import patch

from app.services import character_image_service


class _Headers:
    def __init__(self, content_type: str):
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.headers = _Headers(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int | None = None) -> bytes:
        return self.body


class CharacterReferenceSheetTests(unittest.TestCase):
    @patch.object(character_image_service.settings, "gemini_image_model", "gemini-test-image")
    @patch.object(character_image_service.settings, "google_ai_api_key", "test-google-key")
    @patch("app.services.character_image_service.urlopen")
    def test_generation_conditions_on_reference_image_and_requests_full_sheet(self, urlopen) -> None:
        reference_bytes = b"source-character-image"
        sheet_bytes = b"generated-reference-sheet"
        provider_response = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(sheet_bytes).decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode("utf-8")
        urlopen.side_effect = [
            _Response(reference_bytes, "image/webp"),
            _Response(provider_response),
        ]

        result = character_image_service.generate_character_reference_sheet(
            "Meera",
            "An architect in a linen jacket",
            "https://assets.example.test/meera.webp",
        )

        self.assertEqual(result.data, sheet_bytes)
        self.assertEqual(result.content_type, "image/png")
        self.assertEqual(urlopen.call_count, 2)
        provider_request = urlopen.call_args_list[1].args[0]
        payload = json.loads(provider_request.data)
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0]["inlineData"]["mimeType"], "image/webp")
        self.assertEqual(base64.b64decode(parts[0]["inlineData"]["data"]), reference_bytes)
        prompt = parts[1]["text"]
        self.assertIn("front T-pose", prompt)
        self.assertIn("side profile", prompt)
        self.assertIn("back view", prompt)
        self.assertIn("10 to 15 head-only", prompt)
        self.assertIn("one larger hero portrait", prompt)
        self.assertIn("exact same identity", prompt)


if __name__ == "__main__":
    unittest.main()
