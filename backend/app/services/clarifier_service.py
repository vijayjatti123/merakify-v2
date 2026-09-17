"""Text-only intake. No Director, media providers, background work or job creation."""
import json
import math
import re

from app.agents.llm_client import call_agent
from app.agents.prompts import CLARIFIER, CLARIFIER_REFINE
from app.services import job_service as storage
from app.services.prompt_technique_service import lookup_techniques

QUESTIONS = {
    "product": "What does the product or brand do, and which benefit should this ad demonstrate?",
    "audience": "Who is this ad for?",
    "outcome": "What should viewers remember or do after watching?",
    "execution": "How do you picture the ad playing out on screen?",
    "script_clarity": "Is there an action or speaking role in the script that you want us to interpret a particular way?",
    "tone": "What tone should the video have?",
    "differentiator": "What is the single most important point that should make this video stand out?",
    "constraints": "What must appear in the video, or must be avoided?",
}
MAX_QUESTIONS = 5
DIRECTION_FIELDS = {"audience": "Audience", "takeaway": "Intended takeaway", "product_role": "Product role",
    "execution": "Visual execution", "must_haves": "Must-haves", "exclusions": "Avoid", "open_questions": "Open questions"}
WARNING = "Creative reasoning was unavailable or invalid. Using deterministic fallback; no model answer was assumed."


def _source_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_source_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_source_text(v) for v in value)
    return ""


def _grounded_excerpt(evidence, source):
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    # Quoted excerpts may elide a middle section. Every retained span must
    # still appear verbatim and in order; synonyms/invented claims never count.
    parts = re.split(r"\.{3}|…", evidence.casefold())
    offset = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        position = source.find(part, offset)
        if position < 0:
            return False
        offset = position + len(part)
    return any(p.strip() for p in parts)


def snapshot(row):
    result = {name: getattr(row, name) for name in (
        "session_id", "raw_brief", "known_fields", "turns", "gathered", "confidence",
        "status", "refined_prompt", "revision", "created_at", "updated_at")}
    result["max_questions"] = MAX_QUESTIONS
    result["assessment"] = result["gathered"].get("_assessment", {})
    return result


def _model_context(state):
    return {"raw_brief": state["raw_brief"], "known_fields": state["known_fields"],
        "turns": [{k: t.get(k) for k in ("topic", "question", "answer")} for t in state["turns"]],
        "gathered": {k: v for k, v in state["gathered"].items() if k in QUESTIONS or k in {"_context", "_assessment"}}}


def advance(state):
    turns = state["turns"]
    available = [k for k in QUESTIONS if k not in state["known_fields"]
                 and k not in state["gathered"] and not any(t.get("topic") == k for t in turns)]
    try:
        if state["status"] == "degraded":
            raise ValueError("Already using fallback")
        # Reasoning task, not classification: retain central Sonnet routing.
        state["gathered"]["_reasoning_attempts"] = []
        def record_usage(metadata):
            state["gathered"]["_reasoning_usage"] = metadata
            state["gathered"]["_reasoning_attempts"].append(metadata)
        # Real eight-topic assessment consumed 2,573 thinking tokens before
        # finishing JSON. Reserve room for coverage; one retry shares this budget.
        result = call_agent(CLARIFIER, json.dumps({**_model_context(state), "available_topics": available,
            "topics": list(QUESTIONS)}, ensure_ascii=False, default=str), max_tokens=4096, request_timeout=50,
            truncation_retry_tokens=6144, on_response=record_usage)
        confidence = result["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid confidence")
        coverage = result["coverage"]
        if not isinstance(coverage, dict) or set(coverage) != set(QUESTIONS):
            raise ValueError("Incomplete brief assessment")
        evidence_text = _source_text([state["raw_brief"], state["known_fields"],
            state["gathered"].get("_context", {}), [t.get("answer") for t in turns]]).casefold()
        gaps, unverified = [], []
        for key in QUESTIONS:
            item = coverage[key]
            if not isinstance(item, dict) or item.get("status") not in {"provided", "delegated", "missing", "not_applicable"}:
                raise ValueError("Invalid coverage")
            if item["status"] in {"provided", "delegated"}:
                evidence = item.get("evidence")
                if not _grounded_excerpt(evidence, evidence_text):
                    # An unsupported claim about ONE topic must not discard the
                    # valid understanding and questions for every other topic.
                    item = {"status": "missing", "evidence": "", "question": QUESTIONS[key], "question_source": "fallback"}
                    coverage[key] = item
                    unverified.append(key)
            if item["status"] == "missing":
                gaps.append(key)
        commercial = state["known_fields"].get("content_type", "Ad").casefold() in {"ad", "ugc", "product hero"}
        if commercial and any(coverage[k]["status"] == "not_applicable" for k in ("product", "audience", "outcome")):
            raise ValueError("Ad essentials cannot be skipped")
        summary = result.get("understanding")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("Missing script understanding")
        state["gathered"]["_assessment"] = {"understanding": summary, "coverage": coverage, "unresolved": gaps, "unverified": unverified}
        state["confidence"] = min(confidence, .79) if gaps else confidence
        if not gaps or len(turns) >= MAX_QUESTIONS:
            state["status"] = "ready"
            return
        candidates = [k for k in gaps if k in available]
        if not candidates:
            state["status"] = "ready"
            return
        topic = candidates[0]
        question = coverage[topic].get("question")
        if not isinstance(question, str) or not 10 <= len(question) <= 300 or question.count("?") > 1:
            raise ValueError("Invalid targeted question")
    except Exception as error:
        safe_reasons = {"Already using fallback", "Invalid confidence", "Incomplete brief assessment", "Invalid coverage",
            "Ungrounded coverage claim", "Ad essentials cannot be skipped", "Missing script understanding", "Invalid targeted question"}
        state["gathered"]["_reasoning_failure"] = {"type": type(error).__name__,
            "reason": str(error) if str(error) in safe_reasons else "Provider reasoning failed; see usage/stop metadata when available."}
        state["status"] = "degraded"
        state["confidence"] = 0.0
        state["gathered"]["_assessment"] = {"unresolved": available, "understanding": ""}
        if not available or len(turns) >= MAX_QUESTIONS:
            return
        topic = available[0]
        question = QUESTIONS[topic]
    turns.append({"topic": topic, "question": question, "answer": None,
                  "source": "fallback" if state["status"] == "degraded" else coverage[topic].get("question_source", "model"),
                  "warning": WARNING if state["status"] == "degraded" else None})


def start(db, raw_brief, known_fields, context=None):
    row = storage.create_clarifier_session(db, raw_brief, known_fields, context=context)
    state = snapshot(row)
    advance(state)
    return save(db, state)


def accepted_direction(db, payload):
    """Resolve a reviewed session by ID/revision; do not trust client summaries."""
    if not payload.clarifier_session_id:
        return None
    row = storage.get_clarifier_session(db, payload.clarifier_session_id)
    if not row or row.revision != payload.clarifier_revision or row.status == "cancelled" or not row.refined_prompt:
        raise ValueError("Your refined brief changed. Review it again before creating the job.")
    context = row.gathered.get("_context", {})
    if (context.get("input_mode", "idea") == "script") != (payload.script_text is not None):
        raise ValueError("The input mode changed. Review your production direction again.")
    if payload.script_text is not None:
        if payload.script_text.strip() != row.raw_brief.strip():
            raise ValueError("The script changed. Refine the updated script or use it without the old refinement.")
    elif not payload.brief.strip().startswith(row.refined_prompt.strip()):
        raise ValueError("The brief changed after refinement. Review the updated brief.")
    for key in ("aspect_ratio", "visual_style", "color_grade", "quality", "language", "ai_model"):
        if key in row.known_fields and getattr(payload, key) != row.known_fields[key]:
            raise ValueError("Your video settings changed. Review the refinement again.")
    if set(payload.product_ids) != {p["id"] for p in context.get("products", [])}:
        raise ValueError("Your selected products changed. Review the refinement again.")
    return {"session_id": row.session_id, "revision": row.revision,
        "production_brief": row.refined_prompt, "original_input": row.raw_brief,
        "known_fields": row.known_fields, "context": context,
        "answers": [{"question": t["question"], "answer": t["answer"]} for t in row.turns if t.get("answer")],
        "assessment": row.gathered.get("_assessment", {}), "confidence": row.confidence,
        "degraded": row.status == "degraded"}


def planning_direction(direction):
    """Keep the audit record in storage; do not resend duplicate source/history."""
    if not direction:
        return None
    return {"production_brief": direction["production_brief"], "answers": direction.get("answers", []),
        "known_fields": direction.get("known_fields", {}), "context": direction.get("context", {}),
        "unresolved": direction.get("assessment", {}).get("unresolved", [])}


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
            result = call_agent(CLARIFIER_REFINE, json.dumps({**_model_context(state), "knowledge": knowledge},
                                ensure_ascii=False, default=str), max_tokens=4096, request_timeout=50,
                                truncation_retry_tokens=6144)
            if state["gathered"].get("_context", {}).get("input_mode") == "script":
                notes = result.get("production_direction")
                if not isinstance(notes, dict) or set(notes) != set(DIRECTION_FIELDS) or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 700 for v in notes.values()):
                    raise ValueError("Invalid production direction")
                if sum(len(v.split()) for v in notes.values()) > 350:
                    raise ValueError("Production direction too verbose")
                text = "\n\n".join(f"{label}: {notes[key].strip()}" for key, label in DIRECTION_FIELDS.items())
            else:
                text = result["refined_prompt"]
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("Invalid refinement")
        except Exception:
            state["status"] = "degraded"
            state["confidence"] = 0.0
            text = ("Production direction for the unchanged script:" if state["gathered"].get("_context", {}).get("input_mode") == "script" else state["raw_brief"])
            text += "\n" + "\n".join(f"{t['question']} {t['answer']}" for t in state["turns"] if t.get("answer"))
            state["gathered"]["refinement_warning"] = WARNING
        state["gathered"]["knowledge_ids"] = [k["id"] for k in knowledge]
        # Settings travel in the reviewed handoff and typed job fields, not raw
        # JSON pasted into the customer's editable creative direction.
        state["refined_prompt"] = text.strip()
        if state["status"] != "degraded":
            state["status"] = "refined"
    elif action == "edit":
        if not state["refined_prompt"]:
            raise ValueError("Refine the brief before editing it.")
        state["refined_prompt"] = value
    elif action == "cancel":
        state["status"] = "cancelled"
    return save(db, state)
