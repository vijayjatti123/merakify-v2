import json
import math
import re
import time
from typing import Callable

import anthropic

from app.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=2)

FAST_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-5"


def cinematography_token_budget(scenes: list[dict], target_duration_sec: float) -> int:
    """Output allowance, not a directive to create this many shots.

    Shot count is not known until this call returns. Allow two visual beats per
    scene or one per six seconds, whichever is larger. Reserve 4096 for reasoning
    (the real failed replay spent its whole 4096 there), 768 per verbose shot
    record, plus source-dialogue bytes/2 for multilingual text. These are bounded
    planning estimates, not tokenizer measurements or guaranteed thinking limits.
    """
    estimated_shots = max(1, 2 * len(scenes), math.ceil(max(0, target_duration_sec) / 6))
    dialogue_bytes = sum(len(str(s.get("dialogue_or_vo") or "").encode("utf-8")) for s in scenes)
    allowance = 4096 + 768 * estimated_shots + math.ceil(dialogue_bytes / 2)
    return min(16384, max(8192, math.ceil(allowance / 1024) * 1024))


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
               request_timeout: float | None = None, *,
               truncation_retry_tokens: int | None = None,
               on_response: Callable[[dict], None] | None = None) -> dict:
    from app.agents.execution import current_execution, execute
    scope = current_execution()
    if scope and request_timeout is None:
        return execute(call_agent, system, user_content, dict(fast=fast, max_tokens=max_tokens,
            truncation_retry_tokens=truncation_retry_tokens, on_response=on_response), scope)
    model = FAST_MODEL if fast else REASONING_MODEL
    # Director/Compiler scopes own retries and wall-clock budgets explicitly.
    client = _client if request_timeout is None else _client.with_options(timeout=request_timeout, max_retries=0)
    deadline = time.monotonic() + request_timeout if request_timeout is not None else None
    if truncation_retry_tokens is not None and not max_tokens < truncation_retry_tokens <= 32768:
        raise ValueError("Truncation recovery budget must exceed the initial budget and be at most 32768.")
    request = dict(
        model=model,
        max_tokens=max_tokens,
        # Cache only the unchanged system prefix; job-specific content stays in
        # the user message. Language-specific Architect prompts cache separately.
        # Anthropic ignores this marker below the model's minimum token length;
        # do not pad or rewrite agent instructions just to reach that threshold.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    budgets = [max_tokens] + ([truncation_retry_tokens] if truncation_retry_tokens is not None else [])
    for attempt, budget in enumerate(budgets, 1):
        # Opt-in only. A completed truncated response is not a transport error;
        # retry the unchanged request once, without stacking SDK retries on it.
        attempt_client = client if attempt == 1 else client.with_options(max_retries=0)
        if attempt > 1 and deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Planning response budget expired before truncation recovery. Please retry.")
            attempt_client = client.with_options(timeout=remaining, max_retries=0)
        response = attempt_client.messages.create(**{**request, "max_tokens": budget})
        text = "".join(block.text for block in response.content if block.type == "text")
        will_retry = response.stop_reason == "max_tokens" and attempt < len(budgets)
        if on_response:
            usage = getattr(response, "usage", None)
            on_response({
                "attempt": attempt, "model": model, "max_tokens": budget,
                "message_id": getattr(response, "id", None),
                "request_id": getattr(response, "_request_id", None),
                "stop_reason": response.stop_reason,
                "usage": usage.model_dump(mode="json") if usage is not None else None,
                "visible_text_chars": len(text), "will_retry": will_retry,
            })
        if response.stop_reason != "max_tokens":
            return _extract_json(text)
        if will_retry:
            continue
        if truncation_retry_tokens is not None:
            # Diagnostics live in the trace; never expose partial user dialogue
            # as the customer-facing error or accept a partial shot list.
            raise ValueError("Shot planning exceeded its response budget after one recovery attempt. Please retry planning.")
        raise ValueError(
            f"Model response was cut off at the {max_tokens}-token limit before finishing its "
            f"JSON. Raw text so far ({len(text)} chars): {text[:1500]!r}"
        )
