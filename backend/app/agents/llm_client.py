import json
import re

import anthropic

from app.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=2)

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


def call_agent(system: str, user_content: str, fast: bool = False, max_tokens: int = 2048,
               request_timeout: float | None = None) -> dict:
    model = FAST_MODEL if fast else REASONING_MODEL
    # Compiler-only opt-in: one transport attempt using its remaining wall-clock
    # budget. All existing agents retain the shared 90s timeout / two SDK retries.
    client = _client if request_timeout is None else _client.with_options(timeout=request_timeout, max_retries=0)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        # Cache only the unchanged system prefix; job-specific content stays in
        # the user message. Language-specific Architect prompts cache separately.
        # Anthropic ignores this marker below the model's minimum token length;
        # do not pad or rewrite agent instructions just to reach that threshold.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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
