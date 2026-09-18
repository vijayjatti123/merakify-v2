"""Bounded text calls and private checkpoints; the Director remains the orchestrator."""
import copy
import hashlib
import json
import queue
import threading
import time
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar

import anthropic

_scope = ContextVar("planning_execution", default=None)


def checkpointed_planning(function):
    @wraps(function)
    def wrapped(db, job_id, *args, **kwargs):
        from app.services import job_service
        emit = lambda key, note: job_service.append_event(db, job_id, key, note)
        with planning_execution(db, job_id, emit):
            return function(db, job_id, *args, **kwargs)
    return wrapped


@contextmanager
def planning_execution(db, job_id, emit):
    token = _scope.set((db, job_id, emit, threading.get_ident()))
    try:
        yield
    finally:
        _scope.reset(token)


def current_execution():
    scope = _scope.get()
    # Compiler threads already have their own deadline/usage protocol. Never
    # share the Director's SQLAlchemy session with provider workers.
    return scope if scope and scope[3] == threading.get_ident() else None


def stage_name(system):
    from app.agents import prompts
    for name in ("FORMAT_CLASSIFIER", "SCRIPT_ARCHITECT_FROM_SCRIPT", "CONTINUITY_AGENT",
                 "CINEMATOGRAPHY_AGENT", "CINEMATOGRAPHY_FIX", "CINEMATOGRAPHY_PATCH", "CINEMATOGRAPHY_TRIM",
                 "QA_AGENT", "SHOT_ASSEMBLER"):
        if system == getattr(prompts, name):
            return name.lower()
    return "script_architect" if system.startswith(prompts.SCRIPT_ARCHITECT.split("%s")[0]) else "planning"


def stage_budget(stage):
    return 120.0 if stage.startswith("cinematography") else 90.0


def execute(call, system, content, kwargs, scope):
    from app.services import job_service
    db, job_id, emit, _ = scope
    stage = stage_name(system)
    from app.agents.llm_client import model_route
    from app.agents.output_contracts import contract_for
    contract = contract_for(system, content)
    provider, model = model_route(system, kwargs.get('fast', False))
    identity = ["planning-v2", provider, model] if provider != 'anthropic' else ["planning-v1", model]
    key_parts = [*identity, system, content,
                 {k: v for k, v in kwargs.items() if k != "on_response"}]
    if contract[1] is not None:
        key_parts.append(contract)  # Opt-in schema changes invalidate only affected calls.
    key = hashlib.sha256(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()
    cached = job_service.get_agent_checkpoint(db, job_id, key)
    if cached is not None:
        emit("pipeline_timing", json.dumps({"stage": stage, "phase": "checkpoint_reused", "elapsed_sec": 0}))
        return copy.deepcopy(cached)
    started = time.monotonic()
    # Initial product budgets, not promised provider latency. Both attempts and
    # any truncation recovery share this wall-clock bound.
    budget = stage_budget(stage)
    deadline = started + budget
    responses = queue.Queue()
    callback = kwargs.get("on_response")
    def drain():
        while not responses.empty():
            metadata = responses.get_nowait()
            emit("pipeline_usage", json.dumps({"stage": stage, **metadata}))
            if callback:
                callback(metadata)
    for attempt in (1, 2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Do not reserve time for a timeout retry we deliberately never make.
        # Early transport/rate-limit failures may retry once with what remains.
        attempt_budget = remaining
        emit("pipeline_timing", json.dumps({"stage": stage, "phase": "request_started",
             "attempt": attempt, "budget_sec": round(attempt_budget, 3), "total_budget_sec": budget}))
        completed = queue.Queue()
        def work(options={**kwargs, "request_timeout": attempt_budget, "on_response": responses.put}, out=completed):
            try:
                out.put((True, call(system, content, **options), time.monotonic()))
            except Exception as error:
                out.put((False, error, time.monotonic()))
        threading.Thread(target=work, daemon=True, name="planning-provider").start()
        attempt_end = min(deadline, time.monotonic() + attempt_budget)
        last_heartbeat = time.monotonic()
        try:
            while True:
                wait = attempt_end - time.monotonic()
                if wait <= 0:
                    raise queue.Empty
                try:
                    ok, value, ended = completed.get(timeout=min(wait, 1.0))
                    if ended > attempt_end:
                        raise queue.Empty
                    break
                except queue.Empty:
                    drain()
                    if time.monotonic() - last_heartbeat >= 10:
                        emit("pipeline_timing", json.dumps({"stage": stage, "phase": "waiting",
                             "attempt": attempt, "elapsed_sec": round(time.monotonic() - started, 3)}))
                        last_heartbeat = time.monotonic()
            drain()
        except queue.Empty:
            # An ambiguous timed-out call may still be running remotely. Do not
            # launch a duplicate automatically or let its late result persist.
            emit("pipeline_timing", json.dumps({"stage": stage, "phase": "timeout", "attempt": attempt,
                 "elapsed_sec": round(time.monotonic() - started, 3)}))
            raise TimeoutError("Planning took longer than expected. Your completed steps are saved; please retry.")
        if ok:
            required = {"format_classifier": {"format", "structure", "num_scenes", "duration_target_sec"},
                "script_architect": {"scenes", "logline"}, "script_architect_from_script": {"scenes", "logline"},
                "continuity_agent": {"characters", "locations", "visual_style"},
                "cinematography_agent": {"shots"}, "cinematography_fix": {"shots"}, "cinematography_patch": {"patches"},
                "cinematography_trim": {"shots"}, "qa_agent": {"approved"},
                "shot_assembler": {"transitions"}}.get(stage, set())
            if not isinstance(value, dict) or not required <= value.keys():
                raise ValueError("Planning returned incomplete structured data. Please retry this step.")
            if stage == 'qa_agent':
                try:
                    review = json.loads(content)
                except (ValueError, TypeError):
                    review = None  # Legacy review text has no coverage contract.
                if isinstance(review, dict) and review.get('ad_direction'):
                    from app.services.ad_direction import check_coverage
                    from app.services.story_requirements import check_review
                    # Invalid evidence must not become a checkpoint that every
                    # Retry reuses forever. Persist genuine semantic rejections;
                    # the Director still owns their normal correction loop.
                    check_coverage(value, review.get('shots', []), review.get('approved_story'))
                    if review.get('requirements'):
                        check_review(value, review.get('shots', []), review.get('approved_story'))
            job_service.save_agent_checkpoint(db, job_id, key, value)
            emit("pipeline_timing", json.dumps({"stage": stage, "phase": "completed", "attempt": attempt,
                 "elapsed_sec": round(time.monotonic() - started, 3)}))
            return value
        transient = isinstance(value, (anthropic.APIConnectionError, anthropic.RateLimitError)) or (
            isinstance(value, anthropic.APIStatusError) and value.status_code >= 500)
        emit("pipeline_timing", json.dumps({"stage": stage, "phase": "request_failed", "attempt": attempt,
             "error_type": type(value).__name__, "elapsed_sec": round(time.monotonic() - started, 3),
             "will_retry": bool(transient and attempt == 1 and not isinstance(value, anthropic.APITimeoutError))}))
        if not transient or attempt == 2 or isinstance(value, anthropic.APITimeoutError):
            if isinstance(value, anthropic.APITimeoutError):
                raise TimeoutError("Planning took longer than expected. Your completed steps are saved; please retry.") from value
            raise value
        retry_after = getattr(getattr(value, "response", None), "headers", {}).get("retry-after", "1")
        try:
            backoff = max(1.0, float(retry_after))
        except ValueError:
            backoff = 1.0
        if backoff >= deadline - time.monotonic():
            raise value
        time.sleep(backoff)
    raise TimeoutError("Planning took longer than expected. Your completed steps are saved; please retry.")
