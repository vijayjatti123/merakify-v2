import json
import re

import anthropic

from app.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

FAST_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-5"


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"```json|```", "", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model response. Raw text: {text[:1000]!r}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Model response was not valid JSON ({exc}). Raw text ({len(text)} chars): {text[:1500]!r}"
        ) from exc


def call_agent(system: str, user_content: str, fast: bool = False, max_tokens: int = 2048) -> dict:
    model = FAST_MODEL if fast else REASONING_MODEL
    response = _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    if response.stop_reason == "max_tokens":
        text = "".join(block.text for block in response.content if block.type == "text")
        raise ValueError(
            f"Model response was cut off at the {max_tokens}-token limit before finishing its "
            f"JSON. Raw text so far ({len(text)} chars): {text[:1500]!r}"
        )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)
