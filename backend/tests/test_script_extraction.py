import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import prompts
from app.routes.script_routes import router as scripts_router


class ScriptExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(scripts_router)
        self.client = TestClient(app)

    @patch("app.routes.script_routes.call_agent")
    def test_extracts_two_characters_and_one_location_verbatim(self, call_agent) -> None:
        script_text = (
            "INT. MOONLIGHT CAFE - NIGHT\n"
            "MAYA waits beside the window. ARJUN enters.\n"
            'MAYA: "You came."\nARJUN: "I promised."'
        )
        call_agent.return_value = {
            "characters": ["MAYA", "ARJUN"],
            "locations": ["MOONLIGHT CAFE"],
        }

        response = self.client.post("/api/scripts/extract", json={"script_text": script_text})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "characters": ["MAYA", "ARJUN"],
                "locations": ["MOONLIGHT CAFE"],
            },
        )
        call_agent.assert_called_once_with(
            prompts.SCRIPT_EXTRACTOR,
            script_text,
            fast=True,
            max_tokens=512,
        )

    @patch("app.routes.script_routes.call_agent")
    def test_returns_empty_lists_when_script_has_no_named_entities(self, call_agent) -> None:
        script_text = "A woman crosses a quiet street and enters a small room."
        call_agent.return_value = {"characters": [], "locations": []}

        response = self.client.post("/api/scripts/extract", json={"script_text": script_text})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"characters": [], "locations": []})

    @patch("app.routes.script_routes.call_agent")
    def test_extracts_character_mentioned_only_once(self, call_agent) -> None:
        script_text = "The crowd disperses. Leela pauses at the doorway, then the room falls silent."
        call_agent.return_value = {"characters": ["Leela"], "locations": []}

        response = self.client.post("/api/scripts/extract", json={"script_text": script_text})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"characters": ["Leela"], "locations": []})

    @patch("app.routes.script_routes.call_agent")
    def test_rejects_entity_not_copied_from_source(self, call_agent) -> None:
        call_agent.return_value = {"characters": ["Invented Name"], "locations": []}

        response = self.client.post(
            "/api/scripts/extract",
            json={"script_text": "Maya enters Cafe."},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "script extraction failed"})


if __name__ == "__main__":
    unittest.main()
