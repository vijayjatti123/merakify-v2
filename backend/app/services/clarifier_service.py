"""Text-only intake. No Director, media providers, background work or job creation."""
import json
import math

from app.agents.llm_client import call_agent
from app.agents.prompts import CLARIFIER, CLARIFIER_REFINE
from app.services import job_service as storage
from app.services.prompt_technique_service import lookup_techniques

QUESTIONS = {
    "tone": "What tone should the video have?",
    "differentiator": "What is the single most important point that should make this video stand out?",
    "constraints": "What must appear in the video, or must be avoided?",
}
WARNING = "Creative reasoning was unavailable or invalid. Using deterministic fallback; no model answer was assumed."


def snapshot(row):
    return {name: getattr(row, name) for name in (
        "session_id", "raw_brief", "known_fields", "turns", "gathered", "confidence",
        "status", "refined_prompt", "revision", "created_at", "updated_at")}


def advance(state):
    turns = state["turns"]
    if state["confidence"] >= .8 or len(turns) >= 3:
        if state["status"] != "degraded":
            state["status"] = "ready"
        return
    available = [k for k in QUESTIONS if k not in state["known_fields"]
                 and k not in state["gathered"] and not any(t.get("topic") == k for t in turns)]
    if not available:
        if state["status"] != "degraded":
            state["status"] = "ready"
        return
    try:
        if state["status"] == "degraded":
            raise ValueError("Already using fallback")
        # Reasoning task, not classification: retain central Sonnet routing.
        result = call_agent(CLARIFIER, json.dumps({**state, "available_topics": available},
                            ensure_ascii=False, default=str), max_tokens=1024, request_timeout=45)
        confidence = result["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid confidence")
        topic = result["topic"]
        if topic not in available and topic is not None:
            raise ValueError("Unavailable topic")
        if topic is None and confidence < .8:
            raise ValueError("Missing question at low confidence")
        state["confidence"] = confidence
        if confidence >= .8:
            state["status"] = "ready"
            return
    except Exception:
        state["status"] = "degraded"
        state["confidence"] = 0.0
        topic = available[0]
    turns.append({"topic": topic, "question": QUESTIONS[topic], "answer": None,
                  "source": "fallback" if state["status"] == "degraded" else "model-selected",
                  "warning": WARNING if state["status"] == "degraded" else None})


def start(db, raw_brief, known_fields):
    row = storage.create_clarifier_session(db, raw_brief, known_fields)
    state = snapshot(row)
    advance(state)
    return save(db, state)


def save(db, state):
    return storage.update_clarifier_session(db, state["session_id"], state["revision"],
        {k: state[k] for k in ("raw_brief", "known_fields", "turns", "gathered",
                               "confidence", "status", "refined_prompt")})


def act(db, row, action, value=None):
    state = snapshot(row)
    if state["status"] == "cancelled":
        raise ValueError("This session was cancelled.")
    if action == "answer":
        if state["refined_prompt"] or not state["turns"] or state["turns"][-1]["answer"] is not None:
            raise ValueError("There is no pending question to answer.")
        turn = state["turns"][-1]
        turn["answer"] = value
        state["gathered"][turn["topic"]] = value
        advance(state)
    elif action == "refine":
        knowledge = lookup_techniques(db, state["known_fields"].get("content_type"),
                                     state["known_fields"].get("ai_model"))
        try:
            result = call_agent(CLARIFIER_REFINE, json.dumps({**state, "knowledge": knowledge},
                                ensure_ascii=False, default=str), max_tokens=2048, request_timeout=45)
            text = result["refined_prompt"]
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("Invalid refinement")
        except Exception:
            state["status"] = "degraded"
            state["confidence"] = 0.0
            text = state["raw_brief"] + "\n" + "\n".join(f"{k}: {v}" for k, v in state["gathered"].items())
            state["gathered"]["refinement_warning"] = WARNING
        state["gathered"]["knowledge_ids"] = [k["id"] for k in knowledge]
        state["refined_prompt"] = text.strip() + "\n\nLocked settings:\n" + json.dumps(state["known_fields"], ensure_ascii=False)
        if state["status"] != "degraded":
            state["status"] = "refined"
    elif action == "edit":
        if not state["refined_prompt"]:
            raise ValueError("Refine the brief before editing it.")
        state["refined_prompt"] = value
    elif action == "cancel":
        state["status"] = "cancelled"
    return save(db, state)
