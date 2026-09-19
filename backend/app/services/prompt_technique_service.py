"""Local, tagged knowledge retrieval. NULL on one axis is not a wildcard.

Seeds are supplied research notes, not independently verified provider guarantees.
Stable IDs make startup seeding idempotent without overwriting later curation.
"""
from app.services.speech_mode import is_onscreen_speech
import json

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import PromptTechnique

SEEDS = (
    ("x-kling-word-count", None, "kling", "word_count",
     "Target 50-100 words for this shot; Kling's own guidance shows quality degrading past 150 words, tighter than the general target."),
    ("x-nano-reference-limit", None, "nano_banana", "syntax",
     "Reference images: verify count against the real per-tier limit before submission (see Module AA audit) — do not assume a flat 14."),
    ("x-identity-reinforcement", None, None, "consistency",
     "For shots with a longer compiled prompt, consider reinforcing a key identifying detail again partway through, not just at the start — helps prevent mid-generation drift on longer prompts."),
    ("x-hedra-expression", "dialogue", "hedra", "expression",
     "Use specific micro-expression language (e.g. 'eyes narrowed with real warmth, corners crinkling') rather than generic terms like 'smiling' — improves non-mouth facial performance."),
    ("x-veo-timestamps", None, "veo", "syntax",
     "Veo supports literal timestamp-range multi-shot sequencing, e.g. '[00:00-00:02] description' — different from other models' tagging conventions, do not mix syntax styles."),
)
SOURCE = "User-supplied Module X research notes; original publication URL/date not supplied."


def seed_techniques():
    with SessionLocal() as db:
        for id_, content, model, kind, guidance in SEEDS:
            if db.get(PromptTechnique, id_) is not None:
                continue
            try:
                with db.begin_nested():
                    db.add(PromptTechnique(id=id_, content_type=content, ai_model=model,
                                          technique_type=kind, guidance_text=guidance, source=SOURCE))
                    db.flush()
            except IntegrityError:
                # Another process may seed the same stable ID during startup.
                if db.get(PromptTechnique, id_) is None:
                    raise
        db.commit()


def normalize_tag(value):
    return str(value or "").strip().casefold()


def model_tag(value):
    value = normalize_tag(value).replace(" ", "_")
    for prefix in ("kling", "seedance", "nano_banana", "hedra", "veo", "sora", "wan"):
        if value == prefix or value.startswith(prefix + "_"):
            return prefix
    return value


def lookup_techniques(db, content_type, ai_model):
    # UNION semantics: either exact axis matches OR both axes are global.
    # A model-only Kling entry must not match Seedance merely because content=NULL.
    content, model = normalize_tag(content_type), model_tag(ai_model)
    statement = select(PromptTechnique).where(or_(
        PromptTechnique.content_type == content,
        PromptTechnique.ai_model == model,
        and_(PromptTechnique.content_type.is_(None), PromptTechnique.ai_model.is_(None)),
    )).order_by(PromptTechnique.id)
    return [{key: getattr(row, key) for key in
             ("id", "content_type", "ai_model", "technique_type", "guidance_text", "source")}
            for row in db.scalars(statement)]


def shot_knowledge(payload, emit):
    found = {}
    with SessionLocal() as db:
        for shot in payload["shots"]:
            # Preserve legacy speech guidance; H3 retrieves its own model-tagged facts.
            content = "dialogue" if is_onscreen_speech(shot) else payload["content_type"]
            model = model_tag(payload["ai_model"])
            if is_onscreen_speech(shot) and payload["ai_model"] != "MiniMax H3 Max":
                model = "seedance"
            rows = lookup_techniques(db, content, model)
            found[shot["shot_number"]] = rows
            emit("shot_prompt_compiler", "Prompt technique lookup: " + json.dumps({
                "shot_number": shot["shot_number"], "content_type": content,
                "ai_model": model, "entries": rows}, ensure_ascii=False))
    return found


def knowledge_addendum(group, found):
    sections = []
    for shot in group:
        rows = found.get(shot["shot_number"], [])
        if rows:
            sections.append(f"Shot {shot['shot_number']} only:\n" + "\n".join(
                f"- [{row['id']}; {row['technique_type']}] {row['guidance_text']} Source: {row['source']}"
                for row in rows))
            if any(row["id"] == "x-kling-word-count" for row in rows):
                sections.append("For this Kling target only, the 50-100 total-word range and supplied visual_word_range override the general 100-150-word instruction. All other constraints remain in force.")
    if not sections:
        return ""
    return ("\n\nPROMPT-TECHNIQUE KNOWLEDGE ADDENDUM\n"
            "Apply each entry only to its labeled target shot. Preserve locked facts and output schema.\n"
            + "\n\n".join(sections))
