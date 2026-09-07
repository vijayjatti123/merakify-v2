import json
import re

import anthropic

from app.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# Model routing discipline: only pay for a stronger model where the reasoning
# actually needs it. Classification is cheap and fast; cinematography and QA
# need real judgment.
FAST_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-5"


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"```json|```", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model response: {text[:200]}")
    return json.loads(cleaned[start : end + 1])


def call_agent(system: str, user_content: str, fast: bool = False) -> dict:
    """Call one specialist agent. Returns parsed JSON. Raises on malformed output
    so the orchestrator can decide how to handle a failed step, rather than
    silently passing bad data down the pipeline."""
    model = FAST_MODEL if fast else REASONING_MODEL
    response = _client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)
