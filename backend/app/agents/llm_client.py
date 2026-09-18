import json
import math
import re
import time
from typing import Callable

import anthropic
import httpx

from app.config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=2)

FAST_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-5"


def model_route(system: str, fast: bool = False) -> tuple[str, str]:
    """Only the creative shot-planning family uses the approved Flash route."""
    from app.agents import prompts
    creative = (prompts.CINEMATOGRAPHY_AGENT, prompts.CINEMATOGRAPHY_FIX,
                prompts.CINEMATOGRAPHY_PATCH, prompts.CINEMATOGRAPHY_TRIM)
    if system in creative:
        return 'openrouter', settings.director_model
    return 'anthropic', FAST_MODEL if fast else REASONING_MODEL


def _call_director(system, user_content, model, max_tokens, request_timeout,
                   truncation_retry_tokens, on_response, contract_name, schema):
    if not settings.open_router_api_key:
        raise ValueError('Director provider is not configured. Set OPEN_ROUTER_API_KEY on the server.')
    deadline = time.monotonic() + (request_timeout if request_timeout is not None else 120.0)
    budgets = [max_tokens] + ([truncation_retry_tokens] if truncation_retry_tokens else [])
    for attempt, budget in enumerate(budgets, 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Planning took longer than expected. Please retry.')
        payload = {'model': model, 'max_tokens': budget,
                   'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user_content}],
                   'provider': {'allow_fallbacks': False, 'data_collection': 'deny'}}
        if schema:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': contract_name.replace('-', '_'), 'strict': True, 'schema': schema}}
        # No SDK retries or automatic provider substitution. The existing stage
        # deadline owns late-result rejection; a retry must not duplicate a timeout.
        try:
            with httpx.Client(timeout=remaining) as client:
                response = client.post('https://openrouter.ai/api/v1/chat/completions',
                    headers={'Authorization': 'Bearer ' + settings.open_router_api_key}, json=payload)
        except httpx.TimeoutException as exc:
            raise TimeoutError('Planning took longer than expected. Please retry.') from exc
        except httpx.RequestError as exc:
            raise RuntimeError('Director provider could not be reached. Please retry.') from exc
        if response.status_code != 200:
            # Never copy provider error bodies or user prompts into shared logs.
            raise RuntimeError(f'Director provider returned HTTP {response.status_code}. Please retry.')
        if time.monotonic() > deadline:
            raise TimeoutError('Planning took longer than expected. Please retry.')
        raw = response.json()
        choices = raw.get('choices') or []
        if not choices:
            raise ValueError('Director returned no plan. Please retry.')
        choice = choices[0]
        text = choice.get('message', {}).get('content')
        stop = choice.get('finish_reason')
        will_retry = stop == 'length' and attempt < len(budgets)
        if on_response:
            on_response({'attempt': attempt, 'provider': 'openrouter', 'upstream_provider': raw.get('provider'),
                'model': raw.get('model', model), 'max_tokens': budget, 'message_id': raw.get('id'),
                'stop_reason': stop, 'usage': raw.get('usage'), 'visible_text_chars': len(text or ''),
                'will_retry': will_retry, 'output_contract': contract_name})
        if will_retry:
            continue
        if stop == 'length':
            raise ValueError('Shot planning exceeded its response budget after bounded recovery. Please retry planning.')
        if stop != 'stop' or not isinstance(text, str) or not text.strip():
            raise ValueError('Director could not complete the plan. Please retry.')
        try:
            result = _extract_json(text)
        except ValueError:
            raise ValueError('Director returned incomplete structured data. Please retry.') from None
        if schema:
            from app.agents.output_contracts import validate
            validate(result, schema)
        return result


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
    from app.agents.output_contracts import contract_for, validate
    scope = current_execution()
    if scope and request_timeout is None:
        return execute(call_agent, system, user_content, dict(fast=fast, max_tokens=max_tokens,
            truncation_retry_tokens=truncation_retry_tokens, on_response=on_response), scope)
    provider, model = model_route(system, fast)
    contract_name, schema = contract_for(system, user_content)
    if truncation_retry_tokens is not None and not max_tokens < truncation_retry_tokens <= 32768:
        raise ValueError("Truncation recovery budget must exceed the initial budget and be at most 32768.")
    if provider == 'openrouter':
        return _call_director(system, user_content, model, max_tokens, request_timeout,
                              truncation_retry_tokens, on_response, contract_name, schema)
    # Director/Compiler scopes own retries and wall-clock budgets explicitly.
    client = _client if request_timeout is None else _client.with_options(timeout=request_timeout, max_retries=0)
    deadline = time.monotonic() + request_timeout if request_timeout is not None else None
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
    if schema:
        request['output_config'] = {'format': {'type': 'json_schema', 'schema': schema}}
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
                "output_contract": contract_name,
            })
        if response.stop_reason != "max_tokens":
            if response.stop_reason == 'refusal':
                raise ValueError('The provider could not complete this planning request. Please revise or retry.')
            result = _extract_json(text)
            if schema:
                validate(result, schema)
            return result
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
