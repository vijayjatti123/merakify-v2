"""Text-only intake. No Director, media providers, background work or job creation."""
import json
import copy
import time
import math
import re

from app.agents.llm_client import call_agent
from app.agents.prompts import CLARIFIER, CLARIFIER_REFINE, CLARIFIER_UPDATE
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
QUESTION_OPTIONS = {
    "product": ["Use the product role already described", "Show the main product benefit", "Make the product the visual hero", "Let the Director decide"],
    "audience": ["General audience", "New customers", "Existing customers", "Let the Director decide"],
    "outcome": ["Remember the brand", "Understand the product benefit", "Take action or buy", "Let the Director decide"],
    "execution": ["Cinematic story", "Product-first commercial", "Natural social-media style", "Let the Director decide"],
    "script_clarity": ["Keep the script exactly as written", "Keep dialogue exact; interpret the actions", "Let the Director interpret the scene"],
    "tone": ["Cinematic", "Warm and emotional", "Energetic", "Comedic"],
    "differentiator": ["The product benefit", "The emotional story", "A memorable visual moment", "Let the Director decide"],
    "constraints": ["No additional requirements", "Keep supplied dialogue exactly", "Preserve product and character identity", "Let the Director decide"],
}
MAX_QUESTIONS = 5
DIRECTION_FIELDS = {"audience": "Audience", "takeaway": "Intended takeaway", "product_role": "Product role",
    "execution": "Visual execution", "must_haves": "Must-haves", "exclusions": "Avoid", "open_questions": "Open questions"}
WARNING = "Creative reasoning was unavailable or invalid. Using deterministic fallback; no model answer was assumed."


def _execution_question(raw_brief):
    """Return one bounded production question for physically ambiguous action.

    The reasoning model may identify script_clarity as missing, but it does not
    get to invent user-facing wording.  These deterministic patterns cover the
    high-risk cases that otherwise become impossible geometry downstream.
    """
    text = str(raw_brief or "")
    if (re.search(r"\b(?:helicopter|aircraft|plane|open\s+door|doorway|vehicle|train|car)\b", text, re.I)
            and re.search(r"\b(?:enter|exit|arrive|approach|cross|step|jump|fall|drop|leap|climb|hang|door)\w*\b", text, re.I)):
        return (
            "For the movement around the vehicle or doorway, where is each person at the start, "
            "which safe route do they take, and what physically supports them?",
            ["Keep everyone inside until the stated exit", "Show a clear, safe route", "Let the Director choose a safe route"],
        )
    if re.search(r"\b(?:jump|fall|drop|leap|parachut|climb|hang)\w*\b", text, re.I):
        return (
            "For the jump, fall, or climb, where does the person start, what path do they take, "
            "and where must they end with safe clearance?",
            ["Show the full safe path", "Show only the start and result", "Let the Director choose a safe path"],
        )
    if re.search(r"\b(?:hand(?:s|ed|ing)?\s+\w+(?:\s+\w+){0,4}\s+to|pass(?:es|ed|ing)?|give[sn]?|offer(?:s|ed|ing)?|receive[sd]?)\b", text, re.I):
        return (
            "For the handoff, where are both people and the object at the start, who holds it, "
            "and where should it end?",
            ["Show the complete handoff", "Start after the handoff", "Let the Director choose the clearest staging"],
        )
    if re.search(r"\b(?:enter|exit|arrive|appear|approach|cross|step\s+(?:in|out|through))\w*\b", text, re.I):
        return (
            "For the entrance or exit, where does the person begin, what visible route do they take, "
            "and where do they finish?",
            ["Show the full movement", "Begin after the movement", "Let the Director choose the clearest route"],
        )
    return None


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
    # Existing in-progress sessions may contain model-authored pending wording
    # from an older release. Normalize only the unanswered turn so refresh and
    # the subsequent write both use the current neutral application wording.
    result["turns"] = copy.deepcopy(result["turns"] or [])
    if result["turns"] and result["turns"][-1].get("answer") is None:
        pending_topic = result["turns"][-1].get("topic")
        if pending_topic in QUESTIONS and result["turns"][-1].get("source") != "deterministic_execution":
            result["turns"][-1]["question"] = QUESTIONS[pending_topic]
            result["turns"][-1]["options"] = QUESTION_OPTIONS[pending_topic]
    result["max_questions"] = MAX_QUESTIONS
    result["assessment"] = result["gathered"].get("_assessment", {})
    return result


def _commercial_guidance(state):
    from app.agents.prompts import COMMERCIAL_DIRECTIONS
    return COMMERCIAL_DIRECTIONS.get(state["gathered"].get("_context", {}).get("ad_type"), "")


def _model_context(state):
    return {"commercial_guidance": _commercial_guidance(state),"raw_brief": state["raw_brief"], "known_fields": state["known_fields"],
        "turns": [{k: t.get(k) for k in ("topic", "question", "answer")} for t in state["turns"]],
        "gathered": {k: v for k, v in state["gathered"].items() if k in QUESTIONS or k in {"_context", "_assessment"}}}


def advance(state):
    turns = state["turns"]
    ad_brief = state["gathered"].get("_context", {}).get("ad_brief", {})
    supplied = {topic: ad_brief[field] for field, topic in {
        "audience": "audience", "selling_point": "differentiator", "call_to_action": "outcome",
        "treatment": "execution", "must_preserve": "constraints"}.items() if ad_brief.get(field)}
    available = [k for k in QUESTIONS if k not in supplied and k not in state["known_fields"]
                 and k not in state["gathered"] and not any(t.get("topic") == k for t in turns)]
    try:
        if state["status"] == "degraded":
            raise ValueError("Already using fallback")
        previous = state["gathered"].get("_assessment", {})
        incremental = bool(turns and turns[-1].get("answer") is not None
            and set(previous.get("coverage", {})) == set(QUESTIONS))
        state["gathered"]["_reasoning_attempts"] = []
        started = time.monotonic()
        def record_usage(metadata):
            metadata = {**metadata, "elapsed_seconds": round(time.monotonic() - started, 3),
                "assessment_mode": "update" if incremental else "initial"}
            state["gathered"]["_reasoning_usage"] = metadata
            state["gathered"]["_reasoning_attempts"].append(metadata)
        if incremental:
            # Preserve the source for contradiction checks, but send answers and
            # assessment only once; request deltas rather than full regeneration.
            payload = {"raw_brief": state["raw_brief"], "known_fields": state["known_fields"],
                "commercial_guidance": _commercial_guidance(state),
                "context": state["gathered"].get("_context", {}), "previous_assessment": previous,
                "answers": [{k: t.get(k) for k in ("topic", "question", "answer")} for t in turns],
                "latest_topic": turns[-1]["topic"], "topics": list(QUESTIONS)}
            delta = call_agent(CLARIFIER_UPDATE, json.dumps(payload, ensure_ascii=False, default=str),
                fast=True, max_tokens=2048, request_timeout=25, truncation_retry_tokens=3072,
                on_response=record_usage)
            updates = delta.get("updates")
            if (not isinstance(updates, dict) or set(updates) - set(QUESTIONS)
                    or turns[-1]["topic"] not in updates):
                raise ValueError("Invalid assessment update")
            coverage = copy.deepcopy(previous["coverage"])
            coverage.update(updates)
            result = {"coverage": coverage, "confidence": delta["confidence"],
                "understanding": delta.get("understanding") or previous["understanding"]}
        else:
            result = call_agent(CLARIFIER, json.dumps({**_model_context(state), "available_topics": available,
                "topics": list(QUESTIONS)}, ensure_ascii=False, default=str), max_tokens=4096, request_timeout=50,
                truncation_retry_tokens=6144, on_response=record_usage)
        confidence = result["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid confidence")
        coverage = result["coverage"]
        if not isinstance(coverage, dict) or set(coverage) != set(QUESTIONS):
            raise ValueError("Incomplete brief assessment")
        for topic, value in supplied.items():
            coverage[topic] = {"status": "provided", "evidence": value, "question": QUESTIONS[topic]}
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
        targeted = _execution_question(state["raw_brief"]) if topic == "script_clarity" else None
        # The reasoning model selects the genuine gap. The application owns
        # user-facing wording so it cannot introduce a leading claim, propose
        # a rewrite, or turn one topic into a compound questionnaire.
        question = targeted[0] if targeted else QUESTIONS[topic]
    except Exception as error:
        safe_reasons = {"Already using fallback", "Invalid confidence", "Incomplete brief assessment", "Invalid coverage",
            "Invalid assessment update", "Ungrounded coverage claim", "Ad essentials cannot be skipped", "Missing script understanding", "Invalid targeted question"}
        state["gathered"]["_reasoning_failure"] = {"type": type(error).__name__,
            "reason": str(error) if str(error) in safe_reasons else "Provider reasoning failed; see usage/stop metadata when available."}
        state["status"] = "degraded"
        state["confidence"] = 0.0
        state["gathered"]["_assessment"] = {"unresolved": available, "understanding": ""}
        if not available or len(turns) >= MAX_QUESTIONS:
            return
        topic = available[0]
        question = QUESTIONS[topic]
    targeted = _execution_question(state["raw_brief"]) if topic == "script_clarity" and state["status"] != "degraded" else None
    turns.append({"topic": topic, "question": question,
                  "options": targeted[1] if targeted and question == targeted[0] else QUESTION_OPTIONS[topic], "answer": None,
                  "source": "fallback" if state["status"] == "degraded" else "deterministic_execution" if targeted and question == targeted[0] else coverage[topic].get("question_source", "model"),
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
    from app.commercial import normalized_brief
    if context.get("ad_type", "character") != payload.ad_type or normalized_brief(context.get("ad_brief")) != normalized_brief(payload.ad_brief.model_dump()):
        raise ValueError("Your commercial direction changed. Review the refinement again.")
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
    return {"production_brief": direction["production_brief"], "original_input": direction.get("original_input"),
        "understanding": direction.get("assessment", {}).get("understanding"),
        "confidence": direction.get("confidence"), "degraded": direction.get("degraded", False),
        "answers": direction.get("answers", []),
        "known_fields": direction.get("known_fields", {}), "context": direction.get("context", {}),
        "unresolved": direction.get("assessment", {}).get("unresolved", [])}


def save(db, state):
    return storage.update_clarifier_session(db, state["session_id"], state["revision"],
        {k: state[k] for k in ("raw_brief", "known_fields", "turns", "gathered",
                               "confidence", "status", "refined_prompt")})


def refine_text(state, knowledge):
    """One normal refinement; one format repair within a shared 50s budget."""
    from app.agents.prompts import refinement_system, REFINEMENT_LAYER_VERSION
    from app.agents.output_contracts import contract_for, validate
    context = state['gathered'].get('_context', {})
    script_mode = context.get('input_mode') == 'script'
    system = refinement_system(context.get('ad_type', 'character'), script_mode)
    payload = json.dumps({**_model_context(state), 'knowledge': knowledge}, ensure_ascii=False, default=str)
    _, schema = contract_for(system, payload)
    started = time.monotonic()
    attempts = []
    state['gathered']['refinement_diagnostics'] = {'version': REFINEMENT_LAYER_VERSION, 'attempts': attempts}
    for attempt in range(2):
        remaining = 50 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Refinement time budget exhausted')
        result = None
        violation = 'invalid_json_or_schema'
        try:
            result = call_agent(system, payload, max_tokens=4096, request_timeout=remaining)
            validate(result, schema)
            violation = 'empty_or_overlong_fields'
            if script_mode:
                notes = result['production_direction']
                if any(not v.strip() or len(v) > 700 for v in notes.values()) or sum(len(v.split()) for v in notes.values()) > 350:
                    raise ValueError('Production direction exceeds field contract')
                text = '\n\n'.join(f'{label}: {notes[key].strip()}' for key, label in DIRECTION_FIELDS.items())
            else:
                text = result['refined_prompt'].strip()
                if not text or len(text) > 20000 or (len(state['raw_brief'].split()) <= 350 and len(text.split()) > 350):
                    raise ValueError('Refined brief exceeds field contract')
            attempts.append({'attempt': attempt + 1, 'status': 'valid', 'elapsed_seconds': round(time.monotonic() - started, 3)})
            return text
        except ValueError:
            # Never persist parser exceptions: they can contain private model output.
            attempts.append({'attempt': attempt + 1, 'status': 'invalid_format', 'violation': violation,
                             'elapsed_seconds': round(time.monotonic() - started, 3)})
            if attempt:
                raise
            system += ('\nFORMAT REPAIR: Repair the supplied draft, not a new creative treatment. '
                       'Return only the required JSON fields. Shorten redundant direction to 180-240 words total '
                       'while preserving every supplied factual constraint and dialogue. No preamble.')
            repair_payload = json.loads(payload)
            repair_payload['format_violation'] = violation
            if result is not None:
                repair_payload['draft_to_repair'] = result
            payload = json.dumps(repair_payload, ensure_ascii=False, default=str)
    raise ValueError('Refinement unavailable')


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
            text = refine_text(state, knowledge)
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
