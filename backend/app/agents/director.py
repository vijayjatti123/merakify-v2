import json
import unicodedata
from typing import Callable

from sqlalchemy.orm import Session

from app.agents import prompts
from app.agents.llm_client import call_agent
from app.services import asset_service, character_service, job_service, storage_service, voice_generation_service

EventFn = Callable[[str, str], None]

SHOT_STATUS_PENDING = "pending"
SHOT_STATUS_GENERATING = "generating"
SHOT_STATUS_DONE = "done"
SHOT_STATUS_ERROR = "error"
SHOT_STATUSES = frozenset(
    {
        SHOT_STATUS_PENDING,
        SHOT_STATUS_GENERATING,
        SHOT_STATUS_DONE,
        SHOT_STATUS_ERROR,
    }
)


def _normalize_character_name(name: str) -> str:
    """Return the canonical key used to join names from separate model calls."""
    return unicodedata.normalize("NFC", name)


def _character_name_skeleton(name: str) -> str:
    """Make a conservative fallback key for visually equivalent Indic names.

    NFC cannot unify distinct nasal marks such as Devanagari candrabindu and
    anusvara. Removing only non-spacing marks handles that model variation
    while retaining spacing vowel signs. Callers must use this key only when
    it identifies one unique Continuity character.
    """
    normalized = unicodedata.normalize("NFD", _normalize_character_name(name))
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _reference_name_key(name: str) -> str:
    return _normalize_character_name(name).strip().casefold()


def _apply_continuity_overrides(
    db: Session,
    continuity: dict,
    resolutions: dict | None = None,
    *,
    job_brief: str = "",
    emit: EventFn | None = None,
) -> dict[str, int]:
    """Apply automatic and explicit references in one post-Continuity pass.

    An explicit D2 resolution wins over Module C's automatic name match. Explicit
    ``invent`` choices deliberately suppress automatic matching so the Continuity
    Agent's own proposal remains untouched. With no resolution map, Module C's
    automatic name matching still applies. Only matched vault renderings vary by style.
    """
    resolutions = resolutions or {}
    from app.services import character_style_service

    visual_style = character_style_service.visual_style_from_brief(job_brief)
    renderings = {}

    def rendering(vault_character):
        if vault_character.id not in renderings:
            renderings[vault_character.id] = character_style_service.resolve_character_rendering(
                db, vault_character, visual_style, emit,
            )
        return renderings[vault_character.id]

    explicit_characters = {
        _reference_name_key(name): (name, resolution)
        for name, resolution in resolutions.get("characters", {}).items()
    }
    explicit_locations = {
        _reference_name_key(name): (name, resolution)
        for name, resolution in resolutions.get("locations", {}).items()
    }

    approved_characters = character_service.list_approved_characters(db)
    approved_by_name = {}
    approved_by_id = {}
    for vault_character in approved_characters:
        approved_by_name.setdefault(_reference_name_key(vault_character.name), vault_character)
        approved_by_id[vault_character.id] = vault_character

    stats = {
        "automatic_characters": 0,
        "explicit_characters": 0,
        "explicit_locations": 0,
    }
    consumed_characters: set[str] = set()
    characters = continuity.get("characters", [])
    for index, proposed in enumerate(characters):
        name = proposed.get("name")
        if not isinstance(name, str):
            continue
        key = _reference_name_key(name)
        explicit = explicit_characters.get(key)
        if explicit is not None:
            resolved_name, resolution = explicit
            consumed_characters.add(key)
            if resolution.get("mode") == "invent":
                continue
            vault_character = approved_by_id.get(resolution.get("character_id"))
            if vault_character is None:
                raise ValueError(f'approved character resolution for "{resolved_name}" was not found')
            characters[index] = {
                **proposed,
                "name": resolved_name,
                "description": vault_character.description,
                **rendering(vault_character),
                "voice_id": vault_character.voice_id,
                "voice_sample_ref": vault_character.voice_id,
            }
            stats["explicit_characters"] += 1
            continue

        vault_character = approved_by_name.get(key)
        if vault_character is None:
            continue
        characters[index] = {
            **proposed,
            "description": vault_character.description,
            **rendering(vault_character),
            "voice_id": vault_character.voice_id,
            "voice_sample_ref": vault_character.voice_id,
        }
        stats["automatic_characters"] += 1

    missing_characters = set(explicit_characters) - consumed_characters
    for key in missing_characters:
        resolved_name, resolution = explicit_characters[key]
        if resolution.get("mode") == "invent":
            raise ValueError(f'Continuity omitted AI-invented character "{resolved_name}" from the source script')
        vault_character = approved_by_id.get(resolution.get("character_id"))
        if vault_character is None:
            raise ValueError(f'approved character resolution for "{resolved_name}" was not found')
        characters.append(
            {
                "name": resolved_name,
                "description": vault_character.description,
                **rendering(vault_character),
                "voice_id": vault_character.voice_id,
                "voice_sample_ref": vault_character.voice_id,
            }
        )
        stats["explicit_characters"] += 1

    asset_resolutions = {
        resolution.get("asset_id")
        for _, resolution in explicit_locations.values()
        if resolution.get("mode") == "asset"
    }
    assets_by_id = {
        asset.id: asset for asset in asset_service.list_assets(db) if asset.id in asset_resolutions
    } if asset_resolutions else {}
    consumed_locations: set[str] = set()
    locations = continuity.get("locations", [])
    for index, proposed in enumerate(locations):
        name = proposed.get("name")
        if not isinstance(name, str):
            continue
        key = _reference_name_key(name)
        explicit = explicit_locations.get(key)
        if explicit is None:
            continue
        resolved_name, resolution = explicit
        consumed_locations.add(key)
        if resolution.get("mode") == "invent":
            continue
        asset = assets_by_id.get(resolution.get("asset_id"))
        if asset is None or asset.role != "location":
            raise ValueError(f'location asset resolution for "{resolved_name}" was not found')
        asset_url = storage_service.asset_url(asset.object_key)
        locations[index] = {
            **proposed,
            "name": resolved_name,
            "description": asset.label or proposed.get("description") or asset.filename,
            "asset_id": asset.id,
            "image_url": asset_url,
            "filename": asset.filename,
            "role": asset.role,
            "label": asset.label,
        }
        stats["explicit_locations"] += 1

    missing_locations = set(explicit_locations) - consumed_locations
    for key in missing_locations:
        resolved_name, resolution = explicit_locations[key]
        if resolution.get("mode") == "invent":
            raise ValueError(f'Continuity omitted AI-invented location "{resolved_name}" from the source script')
        asset = assets_by_id.get(resolution.get("asset_id"))
        if asset is None or asset.role != "location":
            raise ValueError(f'location asset resolution for "{resolved_name}" was not found')
        asset_url = storage_service.asset_url(asset.object_key)
        locations.append(
            {
                "name": resolved_name,
                "description": asset.label or asset.filename,
                "asset_id": asset.id,
                "image_url": asset_url,
                "filename": asset.filename,
                "role": asset.role,
                "label": asset.label,
            }
        )
        stats["explicit_locations"] += 1

    return stats


def _attach_voice_refs(
    shots: list[dict],
    characters: list[dict],
    narrator_voice_ref=None,
    *,
    emit: EventFn | None = None,
) -> list[dict]:
    """Attach each shot's character voice references deterministically, in code,
    rather than trusting the model to copy a reference string unchanged across
    several JSON round trips. The Cinematography Agent only has to name which
    characters are in a shot (characters_in_shot); this looks up the actual
    fixed Sarvam catalog voice_sample_ref ID for each name from the continuity
    library. These references are catalog identifiers, never cloned samples.

    A shot can carry dialogue with no character in it at all — voiceover or
    narration, nobody visible on-screen speaking. That case surfaced for real
    the first time this pipeline ran against a real ad brief: has_dialogue
    true, characters_in_shot empty, voice_refs left as an empty dict with
    nowhere for a real voice-generation call to look. Every such shot gets
    the job's one narrator_voice_ref attached under the "Narrator" key here,
    the same deterministic-attachment principle as character voices — never
    left for a model to remember to fill in.

    Also flags every dialogue-carrying shot as experimental: by product
    decision, Indic dialogue audio (Sarvam/ElevenLabs) is passed straight into
    the video model as reference audio, with no dedicated lip-sync pass
    afterward. That's simpler and cheaper, but the video model's own lip-sync
    accuracy for Indic phonemes, and whether it preserves the reference audio
    exactly, are both unverified — see AUDIO_SYNC_DISCLAIMER. This flag is set
    here, in code, on every shot with has_dialogue true, not left to the model
    to remember to mention.
    """
    by_name: dict[str, dict] = {}
    by_skeleton: dict[str, list[dict]] = {}
    for character in characters:
        name = character.get("name")
        if not isinstance(name, str):
            continue
        by_name[_normalize_character_name(name).casefold()] = character
        by_skeleton.setdefault(_character_name_skeleton(name).casefold(), []).append(character)

    for shot in shots:
        names = shot.get("characters_in_shot", [])
        voice_refs = {}
        for name in names:
            if not isinstance(name, str):
                continue

            normalized_name = _normalize_character_name(name)
            character = by_name.get(normalized_name.casefold())
            if character is None:
                candidates = by_skeleton.get(_character_name_skeleton(name).casefold(), [])
                if len(candidates) == 1:
                    character = candidates[0]
                elif not candidates and emit:
                    emit(
                        "cinematography",
                        f'Warning: Shot {shot.get("shot_number", "?")} names character "{name}", '
                        "but no matching character exists in the Continuity plan; no voice reference was attached.",
                    )

            if character is not None:
                voice_refs[normalized_name] = character.get("voice_sample_ref")

        shot["voice_refs"] = voice_refs
        if shot.get("has_dialogue") and not names:
            shot["voice_refs"]["Narrator"] = narrator_voice_ref
        shot["experimental_audio_sync"] = bool(shot.get("has_dialogue"))
        if shot["experimental_audio_sync"]:
            shot["audio_sync_note"] = AUDIO_SYNC_DISCLAIMER
    return shots


AUDIO_SYNC_DISCLAIMER = (
    "Experimental: Indic dialogue audio is sent to the video model as reference "
    "audio directly, with no dedicated lip-sync pass. Accuracy is not assured."
)


def validate_and_correct(
    shots: list[dict],
    characters: list[dict],
    target_duration_sec: float,
    *,
    narrator_voice_ref=None,
    emit: EventFn | None = None,
) -> dict:
    """Run the pipeline's single QA and duration self-correction sequence.

    Both an initial pipeline run and a user revision pass through this function
    so the retry limits and acceptance thresholds cannot drift between paths.
    """

    def notify(agent_key: str, note: str) -> None:
        if emit:
            emit(agent_key, note)

    current_shots = shots

    # Continuity QA Agent, with one autonomous self-correction pass.
    notify("qa", "Checking the shot list for continuity and film-grammar violations...")
    qa = call_agent(
        prompts.QA_AGENT,
        f"Shots: {json.dumps(current_shots)}\nCharacters: {json.dumps(characters)}",
        max_tokens=3072,
    )

    if not qa.get("approved") and qa.get("issues"):
        notify("qa", f"Found {len(qa['issues'])} issue(s) — sending back to Cinematography, no human needed.")
        for issue in qa["issues"]:
            notify("qa", f"Shot {issue['shot_number']}: {issue['problem']}")

        notify("cinematography", "Revising flagged shots per QA feedback...")
        cine = call_agent(
            prompts.CINEMATOGRAPHY_FIX,
            f"Current shots: {json.dumps(current_shots)}\nRequired fixes: {json.dumps(qa['issues'])}",
            max_tokens=4096,
        )
        current_shots = _attach_voice_refs(cine["shots"], characters, narrator_voice_ref, emit=emit)
        notify("cinematography", "Revision complete.")

        notify("qa", "Re-checking the revised shot list...")
        qa = call_agent(
            prompts.QA_AGENT,
            f"Shots: {json.dumps(current_shots)}\nCharacters: {json.dumps(characters)}",
            max_tokens=3072,
        )
        notify("qa", "Approved — continuity holds." if qa.get("approved") else "Residual notes remain; proceeding with best version.")
    else:
        notify("qa", "Approved on first pass — no continuity issues found.")

    # Shot assembly and one autonomous duration-correction pass.
    notify("assembly", "Sequencing shots and choosing transitions...")
    assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(current_shots))
    notify("assembly", f"Runtime locked at {assembly['total_duration_sec']}s.")

    actual = assembly["total_duration_sec"]
    if actual > target_duration_sec * 1.15:
        notify(
            "assembly",
            f"{actual}s overshoots the {target_duration_sec}s target — sending back to Cinematography to trim, no human needed.",
        )
        cine = call_agent(
            prompts.CINEMATOGRAPHY_TRIM,
            f"Current shots: {json.dumps(current_shots)}\nTarget total duration: {target_duration_sec} seconds\n"
            f"Current total: {actual} seconds",
            max_tokens=4096,
        )
        current_shots = _attach_voice_refs(cine["shots"], characters, narrator_voice_ref, emit=emit)
        notify("cinematography", "Trimmed to fit the target runtime.")

        notify("assembly", "Re-sequencing the trimmed shot list...")
        assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(current_shots))
        notify(
            "assembly",
            f"Runtime now {assembly['total_duration_sec']}s (target {target_duration_sec}s)."
            if assembly["total_duration_sec"] <= target_duration_sec * 1.15
            else f"Still {assembly['total_duration_sec']}s after one trim pass; proceeding with best version.",
        )
    else:
        notify("assembly", f"{actual}s is within range of the {target_duration_sec}s target — no trim needed.")

    return {"shots": current_shots, "qa": qa, "assembly": assembly}


def run_pipeline(db: Session, job_id: str) -> None:
    """Runs the full pipeline for one job, synchronously, writing an AgentEvent
    row (and a job status update) after every step so a live SSE stream reading
    the same rows sees progress in near real time. Intended to be called from a
    background task, not from inside the request/response cycle.
    """

    def emit(agent_key: str, note: str) -> None:
        job_service.append_event(db, job_id, agent_key, note)

    try:
        job_service.set_status(db, job_id, "running")
        job = job_service.get_job(db, job_id)
        brief = job.brief
        language = job.language or "English"
        source_script = job.script_text
        resolutions = json.loads(job.resolutions_json) if job.resolutions_json else None

        # 1. Format Classifier — cheap/fast model, this step is pure classification.
        emit("format", "Reading the request, choosing format and structure...")
        fmt = call_agent(prompts.FORMAT_CLASSIFIER, brief, fast=True)
        emit("format", f"Classified as {fmt['format']}, {fmt['structure']} structure, {fmt['num_scenes']} scenes.")

        # 2. Script Architect — pasted scripts use a distinct preservation
        # prompt; the original no-script call remains unchanged.
        emit("script", "Structuring the pasted script without rewriting it..." if source_script else "Writing scene breakdown...")
        if source_script:
            resolved_names = resolutions or {"characters": {}, "locations": {}}
            script = call_agent(
                prompts.SCRIPT_ARCHITECT_FROM_SCRIPT,
                f"Source script:\n{source_script}\n\nProduction format: {fmt['format']}\n"
                f"Target duration: {fmt['duration_target_sec']} seconds\n"
                f"Named characters: {json.dumps(list(resolved_names.get('characters', {})), ensure_ascii=False)}\n"
                f"Named locations: {json.dumps(list(resolved_names.get('locations', {})), ensure_ascii=False)}",
            )
        else:
            script = call_agent(
                prompts.SCRIPT_ARCHITECT % language,
                f"Brief: {brief}\nFormat: {fmt['format']}\nStructure: {fmt['structure']}\nNumber of scenes: {fmt['num_scenes']}",
            )
        emit("script", f"Logline locked: \"{script['logline']}\"")

        # 3. Visual Continuity Agent — builds the reference library BEFORE any
        # shot is planned, so every later step can be checked against it.
        emit("continuity_plan", "Building the reference asset library before any shot is planned...")
        continuity_input = json.dumps(script["scenes"])
        if source_script:
            continuity_input += (
                "\nEvery named script entity below must have one matching Continuity entry; preserve each name exactly:\n"
                f"Characters: {json.dumps(list((resolutions or {}).get('characters', {})), ensure_ascii=False)}\n"
                f"Locations: {json.dumps(list((resolutions or {}).get('locations', {})), ensure_ascii=False)}"
            )
        continuity = call_agent(prompts.CONTINUITY_AGENT, continuity_input)
        override_stats = _apply_continuity_overrides(db, continuity, resolutions, job_brief=brief, emit=emit)
        assigned_voice_count = voice_generation_service.assign_missing_voice_ids(continuity)
        if override_stats["automatic_characters"]:
            emit(
                "continuity_plan",
                f"Applied {override_stats['automatic_characters']} approved Character Vault reference(s) by exact name.",
            )
        if override_stats["explicit_characters"] or override_stats["explicit_locations"]:
            emit(
                "continuity_plan",
                f"Applied {override_stats['explicit_characters']} explicit character and "
                f"{override_stats['explicit_locations']} explicit location resolution(s).",
            )
        emit(
            "continuity_plan",
            f"Locked {len(continuity['characters'])} character(s), {len(continuity['locations'])} location(s) as identity anchors.",
        )

        # 4. Cinematography Agent — the largest output in the pipeline (one
        # object per shot, several fields each), so it gets a bigger token
        # budget than the default rather than risking truncation. It also
        # needs the target runtime explicitly — without it, there's nothing
        # stopping the total from drifting well past what was asked for.
        emit("cinematography", "Assigning camera, lens and lighting per shot...")
        cinematography_input = (
            f"Scenes: {json.dumps(script['scenes'])}\nCharacters: {json.dumps(continuity['characters'])}"
        )
        if source_script:
            cinematography_input += f"\nLocations: {json.dumps(continuity['locations'])}"
        cinematography_input += f"\nTarget total duration: {fmt['duration_target_sec']} seconds"
        cine = call_agent(
            prompts.CINEMATOGRAPHY_AGENT,
            cinematography_input,
            max_tokens=4096,
        )
        assigned_voice_count += voice_generation_service.assign_missing_voice_ids(continuity, cine["shots"])
        if assigned_voice_count:
            emit(
                "continuity_plan",
                f"Assigned {assigned_voice_count} fixed Sarvam catalog voice reference(s) for this job.",
            )
        cine["shots"] = _attach_voice_refs(
            cine["shots"],
            continuity["characters"],
            continuity.get("narrator_voice_ref"),
            emit=emit,
        )
        dialogue_shots = sum(1 for s in cine["shots"] if s.get("has_dialogue"))
        cutaway_shots = len(cine["shots"]) - dialogue_shots
        emit(
            "cinematography",
            f"Drafted {len(cine['shots'])} shot(s): {dialogue_shots} carrying dialogue, "
            f"{cutaway_shots} silent cutaway/reaction shot(s).",
        )

        # 5-7. Both initial runs and user revisions use this one QA/duration
        # self-correction implementation so their behavior cannot diverge.
        validated = validate_and_correct(
            cine["shots"],
            continuity["characters"],
            fmt["duration_target_sec"],
            narrator_voice_ref=continuity.get("narrator_voice_ref"),
            emit=emit,
        )
        cine["shots"] = _attach_voice_refs(
            validated["shots"], continuity["characters"], continuity.get("narrator_voice_ref")
        )
        for shot in cine["shots"]:
            shot["status"] = SHOT_STATUS_PENDING
        qa = validated["qa"]
        assembly = validated["assembly"]

        # TODO (next milestone, not this skeleton): fan out here to real asset
        # generation — one call per character/location in continuity, run
        # concurrently with asyncio.gather rather than sequentially.
        #
        # TODO (rendering, depends on the above): for each shot, call the
        # chosen video model. For shots with has_dialogue true, pass the
        # character's voice_sample_ref audio (Sarvam/ElevenLabs) directly as
        # the model's reference-audio input (e.g. Seedance's reference_audios)
        # so the model drives both video and lip movement from that audio in
        # one call. By product decision, there is NO separate lip-sync pass
        # (Sync Labs/HeyGen) in the default pipeline — every shot with
        # experimental_audio_sync true (set in _attach_voice_refs) should
        # surface AUDIO_SYNC_DISCLAIMER wherever it's shown to the user,
        # since neither the video model's lip-sync accuracy for Indic
        # phonemes nor its preservation of the input audio's exact rhythm is
        # verified. Revisit this the moment real output is reviewable: if it
        # holds up, keep it; if it visibly drifts, that's the signal to add a
        # dedicated lip-sync step back in for dialogue shots specifically,
        # not to silently patch the disclaimer instead of the pipeline.

        result = {
            "aspect_ratio": job.aspect_ratio,
            "quality": job.quality,
            "language": language,
            "ai_model": job.ai_model,
            "format": fmt,
            "script": script,
            "continuity": continuity,
            "shots": cine["shots"],
            "generation_approved": False,
            "assembly": assembly,
            "qa": qa,
        }
        if source_script:
            result["source_script_text"] = source_script
        job_service.set_result(db, job_id, result)
        job_service.set_status(db, job_id, "done")

    except Exception as exc:  # noqa: BLE001 — surface any failure to the job record
        job_service.set_status(db, job_id, "error", error_message=str(exc))
        emit("error", f"Pipeline failed: {exc}")
