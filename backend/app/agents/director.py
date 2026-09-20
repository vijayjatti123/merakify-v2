from app.services.planning_contract import creative_references, camera_options, render_camera_summaries, check_mechanics
from app.services.planning_patch import patch_permissions, apply_patch_response, protect_unflagged, apply_insertion_response
from app.services import ad_direction
from app.services import story_requirements
from app.services.speech_mode import is_voiceover
from app.services.camera_direction import check_plan as check_camera_plan
import json
import copy
import queue
import threading
import time
import unicodedata
from typing import Callable

from sqlalchemy.orm import Session

from app.agents import prompts
from app.agents.dialogue_integrity import protected_dialogue, restore_protected, screen_issues, warn_dialogue_loss
from app.agents.llm_client import call_agent, cinematography_token_budget
from app.agents.execution import checkpointed_planning
from app.services import asset_service, character_service, job_service, storage_service, voice_generation_service
from app.services.shot_prompt_compiler import compile_shot_prompts
from app.services.still_frame_service import generate_still_frames

EventFn = Callable[[str, str], None]


def prompt_polish_context(ad_type: str, *, brief: str, language: str,
                          duration_seconds: float | None, aspect_ratio: str,
                          visual_style: str | None, color_grade: str | None,
                          quality: str | None, video_model: str | None) -> str:
    """Assemble the compact, server-owned prompt layers for the Director.

    This is intentionally deterministic: Clarifier supplies user intent and
    the existing Director remains the only creative model call. The assembled
    contract gives vague briefs a concrete default while keeping typed facts
    authoritative and provider details in the later adapter.
    """
    preset = prompts.PROMPT_POLISH_PRESETS.get(ad_type, prompts.PROMPT_POLISH_PRESETS["character"])
    facts = {
        "language": language,
        "duration_seconds": duration_seconds if duration_seconds is not None else "choose from explicit brief; otherwise classifier target",
        "aspect_ratio": aspect_ratio,
        "visual_style": visual_style or "Natural",
        "color_grade": color_grade or "None",
        "quality": quality or "standard",
        "video_model": video_model or "provider default",
    }
    return ("\n\n" + prompts.PROMPT_POLISH_BASE + "\n" + preset
            + "\nLOCKED PRODUCTION FACTS (code-owned; do not reinterpret):\n"
            + json.dumps(facts, ensure_ascii=False)
            )

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
            if visual_style == "Natural" and getattr(vault_character, "reference_sheet_url", None):
                renderings[vault_character.id]["reference_sheet_url"] = vault_character.reference_sheet_url
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
                "voice_assignment": "vault",
                "character_id": vault_character.id,
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
            "voice_assignment": "vault",
            "character_id": vault_character.id,
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
                "voice_assignment": "vault",
                "character_id": vault_character.id,
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

        if is_voiceover(shot):
            shot["speaker_label"] = "Narrator"
        elif shot.get("speaker_name"):
            shot["speaker_label"] = shot["speaker_name"]
        if shot.get("speaker_name") in voice_refs and not is_voiceover(shot):
            voice_refs = {shot["speaker_name"]: voice_refs[shot["speaker_name"]]}
        shot["voice_refs"] = {"Narrator": narrator_voice_ref} if is_voiceover(shot) else voice_refs
        shot["speech_mode"] = "voiceover" if is_voiceover(shot) else "onscreen" if shot.get("has_dialogue") else "none"
        shot["experimental_audio_sync"] = bool(shot.get("has_dialogue")) and not is_voiceover(shot)
        if shot["experimental_audio_sync"]:
            shot["audio_sync_note"] = AUDIO_SYNC_DISCLAIMER
        else:
            shot.pop("audio_sync_note", None)
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
    source_script_text: str | None = None,
    defer_audio_assembly: bool = True,
    emit: EventFn | None = None,
    minimum_shot_seconds: float | None = None,
    ad_direction_plan: dict | None = None,
    approved_story: dict | None = None,
    semantic_review: bool | None = None,
) -> dict:
    """Run the pipeline's single QA and duration self-correction sequence.

    Both an initial pipeline run and a user revision pass through this function
    so the retry limits and acceptance thresholds cannot drift between paths.
    """

    # Legacy/manual callers without an approved story-direction contract keep
    # the fast code-only review. Fresh planned ads carry ad_direction_plan and
    # receive the bounded semantic feasibility review below.
    if semantic_review is None:
        semantic_review = bool(ad_direction_plan)
    if not semantic_review or not ad_direction_plan:
        from app.services.director_review import review, timeline
        current_shots = render_camera_summaries(shots)
        verdict = review(current_shots, characters, minimum_shot_seconds,
            (approved_story or {}).get("production_context", {}).get("commercial"))
        for shot in current_shots:
            shot["review_mode"] = "user"
        if verdict["approved"]:
            ad_direction.accept_shots(current_shots)
        return {"shots": current_shots, "qa": verdict,
                "assembly": timeline(current_shots, provisional=any(s.get("has_dialogue") for s in current_shots))}

    def notify(agent_key: str, note: str) -> None:
        if emit:
            emit(agent_key, note)

    current_shots = render_camera_summaries(shots)
    story_outline = {k:v for k,v in (approved_story or {}).items() if k != 'production_context'}
    direction_context = ("\nad_direction: " + json.dumps(ad_direction_plan)
                         + "\napproved_story: " + json.dumps(story_outline)) if ad_direction_plan else ""
    if ad_direction_plan:
        direction_context += "\nSource-linked requirements: " + json.dumps(story_requirements.review_context(approved_story))
        direction_context += "\nCode-owned duration constraints: " + json.dumps({
            'minimum_shot_seconds': minimum_shot_seconds, 'silent_max_seconds': 9,
            'voiceover_max_seconds': 15, 'target_total_seconds': target_duration_sec,
            'rule': 'Never delete speech or approved story beats to force the target; no new shot below the model minimum.'})
    # The eight-shot audit spent 5084/5120 tokens thinking, then retried with
    # only 37 seconds left. Give complex plans the existing recovery allowance
    # up front, under the unchanged 90-second deadline. Recalculate on revision
    # because a story repair may add shots. Legacy QA keeps its existing budget.
    def qa_options(reviewed_shots):
        tokens = min(8192, 4096 + 512 * len(reviewed_shots)) if ad_direction_plan else 3072
        options = {'max_tokens': tokens}
        if ad_direction_plan and tokens < 8192:
            options['truncation_retry_tokens'] = 8192
        return options
    planning_fields = ('shot_number', 'scene_number', 'description', 'dialogue_text', 'has_dialogue',
        'speech_mode', 'speaker_name', 'characters_in_shot', 'opening_characters', 'shot_direction',
        'direction_version', 'camera_angle', 'camera_direction', 'camera_movement', 'lens',
        'lighting', 'composition_note', 'duration_sec', 'dialogue_audio_duration_sec',
        'state_at_shot_start', 'state_at_shot_end')
    def planning_view(shot):
        """Remove signed URLs and generated media state from planning calls."""
        return {key: shot[key] for key in planning_fields if key in shot}
    def review_content(reviewed_shots):
        if ad_direction_plan:
            # QA needs the whole story and decisions, not signed URLs, previous
            # generated prompts, images, playback metadata or reference hashes.
            reviewed_shots = [planning_view(shot) for shot in reviewed_shots]
            refs = creative_references(characters)
            return json.dumps({'shots': reviewed_shots, 'characters': refs,
                'ad_direction': ad_direction_plan, 'approved_story': story_outline,
                **story_requirements.review_context(approved_story),
                'mechanical_findings': code_review(reviewed_shots).get('issues', [])})
        else:
            refs = characters
        return f"Shots: {json.dumps(reviewed_shots)}\nCharacters: {json.dumps(refs)}" + direction_context
    # Contract errors join the existing bounded QA/FIX loop, not another call.
    def code_review(reviewed_shots):
        return ad_direction.check_plan(check_mechanics(check_camera_plan({'approved': True, 'issues': []}, reviewed_shots),
            reviewed_shots, characters, minimum_shot_seconds), reviewed_shots)

    def mechanical_review(verdict, reviewed_shots):
        if ad_direction_plan:
            verdict = story_requirements.check_review(verdict, reviewed_shots, approved_story)
            verdict = story_requirements.derive_scene_coverage(verdict, approved_story)
            verdict = ad_direction.check_coverage(verdict, reviewed_shots, approved_story)
        findings = code_review(reviewed_shots).get('issues', [])
        issues = list(verdict.get('issues', []))
        issues.extend(i for i in findings if not any(
            old.get('shot_number') == i['shot_number'] and old.get('code') == i.get('code') for old in issues))
        return {**verdict, 'approved': bool(verdict.get('approved')) and not issues, 'issues': issues}

    # Repair code-detectable defects BEFORE semantic QA. The old order paid for
    # a complete story review, discovered a camera enum afterward, repaired one
    # field, then paid for the same complete review again. Semantic QA now sees
    # one mechanically valid plan while retaining its independent judgment.
    protected = protected_dialogue(current_shots, source_script_text)
    preflight_issues = code_review(current_shots).get('issues', [])
    if preflight_issues:
        notify('cinematography', f"Correcting {len(preflight_issues)} technical planning issue(s) before continuity review...")
        permissions = patch_permissions(current_shots, preflight_issues)
        if permissions:
            targets = set(permissions)
            by_number = {shot['shot_number']: shot for shot in current_shots}
            ordered = [shot['shot_number'] for shot in current_shots]
            neighbors = set()
            for number in targets:
                index = ordered.index(number)
                neighbors.update(ordered[max(0, index - 1):index])
                neighbors.update(ordered[index + 1:index + 2])
            compact = {
                'target_shots': [planning_view(by_number[number]) for number in ordered if number in targets],
                'readonly_neighbors': [{key: by_number[number].get(key) for key in
                    ('shot_number', 'scene_number', 'description', 'camera_angle',
                     'state_at_shot_start', 'state_at_shot_end')}
                    for number in ordered if number in neighbors],
                'required_fixes': preflight_issues,
                'allowed_fields': permissions,
                'camera_options': camera_options(),
                'ad_direction': ad_direction_plan,
            }
            patch_response = call_agent(prompts.CINEMATOGRAPHY_PATCH,
                json.dumps(compact, ensure_ascii=False),
                max_tokens=min(4096, 1536 + 256 * len(permissions)))
            current_shots = apply_patch_response(current_shots, patch_response, permissions)
        else:
            # Rare cross-field failures (for example an invalid speaker/cast
            # relationship) need the existing complete-plan correction, still
            # before QA and still protected by dialogue/unflagged-shot guards.
            cine = call_agent(prompts.CINEMATOGRAPHY_FIX,
                f"Current shots: {json.dumps(current_shots)}\nRequired mechanical fixes: {json.dumps(preflight_issues)}"
                f"\ncamera_options: {json.dumps(camera_options())}" + direction_context,
                max_tokens=cinematography_token_budget([{}] * ((len(current_shots) + 1) // 2), target_duration_sec))
            if ad_direction_plan:
                protect_unflagged(current_shots, cine['shots'], preflight_issues)
            repaired, violations = restore_protected(current_shots, cine['shots'], protected, notify)
            if violations:
                raise ValueError('Technical correction could not preserve approved dialogue. Retry planning.')
            current_shots = repaired
            if any(shot.get('direction_version') == 1 for shot in shots):
                for shot in current_shots:
                    shot['direction_version'] = 1
                    if all(key in shot for key in ad_direction.DIRECTION_FIELDS):
                        shot['direction_source'] = ad_direction.source_key(shot)
        current_shots = render_camera_summaries(_attach_voice_refs(
            current_shots, characters, narrator_voice_ref, emit=emit))
        remaining_mechanics = code_review(current_shots).get('issues', [])
        if remaining_mechanics:
            raise ValueError('Technical planning correction remained invalid. Your story is saved; retry planning.')
        notify('cinematography', 'Technical plan validated; starting one independent continuity review.')

    # Continuity QA Agent, with one autonomous semantic self-correction pass.
    notify("qa", "Checking the shot list for continuity and film-grammar violations...")
    qa = call_agent(
        prompts.QA_AGENT,
        review_content(current_shots),
        **qa_options(current_shots),
    )

    qa = mechanical_review(qa, current_shots)
    def screen_review_issues(verdict):
        findings = verdict.get('issues', [])
        # An insertion's shot_number identifies a boundary, not a request to
        # edit its anchor's dialogue. Code permits only a new silent shot; all
        # edits of existing shots still pass through unchanged Module K guards.
        insertions = [i for i in findings if ad_direction_plan and i.get('repair_kind') == 'insert_after']
        if any(i.get('repair_fields', []) for i in insertions):
            raise ValueError('Insertion cannot authorize edits to its anchor. Retry planning.')
        accepted, verified, rejected, limited = screen_issues(current_shots,
            [i for i in findings if i not in insertions], protected, notify)
        return [*accepted, *insertions], verified, rejected, limited
    issues, verified, rejected, limitations = screen_review_issues(qa)
    qa = {**qa, "issues": issues}
    if rejected or limitations:
        qa["approved"] = not issues and not limitations
    loss_warnings = []

    if not qa.get("approved") and qa.get("issues"):
        notify("qa", f"Found {len(qa['issues'])} issue(s) — sending back to Cinematography, no human needed.")
        for issue in qa["issues"]:
            notify("qa", f"Shot {issue['shot_number']}: {issue['problem']}")

        notify("cinematography", "Revising flagged shots per QA feedback...")
        insertion_issues = [i for i in qa['issues'] if i.get('repair_kind') == 'insert_after'] if ad_direction_plan else []
        field_issues = [i for i in qa['issues'] if i not in insertion_issues]
        permissions = patch_permissions(current_shots, field_issues) if field_issues else {}
        insert_anchors = sorted({i['shot_number'] for i in insertion_issues})
        if insert_anchors and any(n not in {s['shot_number'] for s in current_shots} for n in insert_anchors):
            raise ValueError('Story repair targeted an unknown insertion boundary. Retry planning.')
        if permissions or (insert_anchors and not field_issues):
            notify('cinematography', 'Applying targeted field corrections; other shot content is preserved.')
            patch_response = call_agent(
                prompts.CINEMATOGRAPHY_PATCH,
                f"Read-only plan: {json.dumps(current_shots)}\nRequired fixes: {json.dumps(qa['issues'])}"
                + f"\nallowed_fields: {json.dumps(permissions)}\ncamera_options: {json.dumps(camera_options())}"
                + (f"\nallowed_insert_after: {json.dumps(insert_anchors)}" if insert_anchors else '') + direction_context,
                max_tokens=min(8192, 2048 + 256 * len(permissions) + 2048 * len(insert_anchors)),
            )
            if insert_anchors:
                repaired, number_map = apply_insertion_response(current_shots, patch_response, permissions, insert_anchors)
                # Preserve Module K's identity correspondence when code assigns
                # display ordinals after insertion. Existing speech is unchanged.
                current_shots = [{**s, 'shot_number': number_map[s['shot_number']]} for s in current_shots]
                protected = {number_map[n]: {**s, 'shot_number': number_map[n]} for n,s in protected.items()}
                verified = {number_map[n] for n in verified}
                notify('cinematography', f'Inserted {len(insert_anchors)} silent story beat(s); existing shot content retained.')
                cine = {'shots': repaired}
            else:
                cine = {'shots': apply_patch_response(current_shots, patch_response, permissions)}
        else:
            # Directed records carry opening/performance/end facts. Reuse the
            # existing batch-size allowance instead of the old flat 4096 cap.
            repair_tokens = cinematography_token_budget([{}] * ((len(current_shots) + 1) // 2), target_duration_sec) if ad_direction_plan else 4096
            cine = call_agent(
                prompts.CINEMATOGRAPHY_FIX,
                f"Current shots: {json.dumps(current_shots)}\nRequired fixes: {json.dumps(qa['issues'])}\ncamera_options: {json.dumps(camera_options())}" + direction_context
                + (f"\nProtected user-scripted dialogue shot numbers: {json.dumps(list(protected))}. "
                   "Keep these shots present and preserve their dialogue_text, has_dialogue and scene_number exactly. "
                   "Only apply compatible visual corrections." if protected else ""),
                max_tokens=repair_tokens,
            )
            if ad_direction_plan:
                protect_unflagged(current_shots, cine['shots'], qa['issues'])
        loss_warnings = warn_dialogue_loss(current_shots, cine["shots"], verified, notify)
        revised, violations = restore_protected(current_shots, cine["shots"], protected, notify)
        # A full-plan correction must not accidentally downgrade directed shots
        # to legacy plans. Missing direction is caught on the semantic recheck.
        directed_revision = any(s.get('direction_version') == 1 for s in current_shots)
        for shot in revised:
            if directed_revision:
                shot['direction_version'] = 1
                if all(k in shot for k in ad_direction.DIRECTION_FIELDS):
                    shot['direction_source'] = ad_direction.source_key(shot)
        limitations.extend(violations)
        current_shots = render_camera_summaries(_attach_voice_refs(revised, characters, narrator_voice_ref, emit=emit))
        notify("cinematography", "Revision complete.")

        notify("qa", "Re-checking the revised shot list...")
        qa = call_agent(
            prompts.QA_AGENT,
            review_content(current_shots),
            **qa_options(current_shots),
        )
        qa = mechanical_review(qa, current_shots)
        remaining, _, rejected_again, blocked_again = screen_review_issues(qa)
        rejected.extend(rejected_again)
        limitations.extend(blocked_again)
        qa = {**qa, "issues": remaining}
        if rejected_again or blocked_again:
            qa["approved"] = not remaining and not limitations
        if limitations:
            qa["approved"] = False
        notify("qa", "Approved — continuity holds." if qa.get("approved") else
               "Direction still needs correction; media generation remains stopped." if any(s.get('direction_version') == 1 for s in current_shots)
               else "Residual notes remain; proceeding with best version.")
    else:
        notify("qa", "User-scripted dialogue protected; unresolved QA limitation remains." if limitations
               else "No actionable QA issues remain after deterministic validation." if rejected
               else "Approved on first pass — no continuity issues found.")

    if rejected:
        qa["rejected_claims"] = rejected
    if limitations:
        qa["approved"] = False
        qa["issues"] = [*qa["issues"], *limitations]
        qa["dialogue_protection_limitations"] = limitations
    if loss_warnings:
        qa["dialogue_loss_warnings"] = loss_warnings
    if any(s.get('direction_version') == 1 for s in current_shots):
        if not qa.get('approved'):
            raise ValueError("The directed shot plan still has unresolved checks. Your story is saved; retry planning.")
        ad_direction.accept_shots(current_shots)

    # Dialogue timing is provisional until approval triggers real decoded audio.
    # Module K's QA/FIX logic above is unchanged; only assembly is deferred.
    if defer_audio_assembly and any(shot.get("has_dialogue") for shot in current_shots):
        notify("assembly", "Assembly and duration self-check deferred until dialogue audio is decoded and corrected after approval.")
        return {"shots": current_shots, "qa": qa, "assembly": {
            "total_duration_sec": sum(float(s.get("duration_sec", 0)) for s in current_shots),
            "transitions": [], "provisional": True,
        }}
    assembled = assemble_shots(current_shots, characters, target_duration_sec, narrator_voice_ref=narrator_voice_ref, emit=emit)
    return {**assembled, "qa": qa}


def assemble_shots(shots, characters, target_duration_sec, *, narrator_voice_ref=None, emit=None):
    """Existing assembly/duration sequence; dialogue jobs call this only after audio."""
    if shots and all(s.get("review_mode") == "user" for s in shots):
        from app.services.director_review import timeline
        return {"shots": shots, "assembly": timeline(shots)}
    current_shots = shots
    def notify(key, note):
        if emit:
            emit(key, note)
    # Shot assembly and one autonomous duration-correction pass.
    notify("assembly", "Sequencing shots and choosing transitions...")
    assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(current_shots))
    assembly["total_duration_sec"] = sum(float(s["duration_sec"]) for s in current_shots)
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
            f"Current total: {actual} seconds\n"
            "Shots with dialogue_audio_duration_sec already have real audio. Keep their shot_number, dialogue, duration, and order fixed. Trim only silent shots; never invent dialogue.",
            max_tokens=4096,
        )
        measured = {s["shot_number"]: s for s in current_shots if s.get("dialogue_audio_duration_sec") is not None}
        originals = {s["shot_number"]: s for s in current_shots}
        proposed = {s["shot_number"]: s for s in cine["shots"]}
        directed = any(s.get('direction_version') == 1 for s in current_shots)
        invalid_directed_trim = directed and (
            [s['shot_number'] for s in cine['shots']] != [s['shot_number'] for s in current_shots]
            or any(any(s.get(field) != originals[s['shot_number']].get(field)
                       for field in ('description', 'dialogue_text', 'has_dialogue', 'scene_number',
                                     'characters_in_shot', 'opening_characters', 'state_at_shot_start', 'state_at_shot_end', 'shot_direction'))
                   for s in cine['shots'] if s['shot_number'] in originals))
        invalid_audio_trim = measured and (
            [s["shot_number"] for s in cine["shots"] if s.get("has_dialogue")] != list(measured)
            or any(s["shot_number"] not in originals for s in cine["shots"])
            or any(number not in proposed or any(proposed[number].get(field) != original.get(field)
                   for field in ("dialogue_text", "has_dialogue", "duration_sec", "scene_number", "characters_in_shot"))
                   for number, original in measured.items()))
        if invalid_directed_trim:
            notify("assembly", "Duration trim rejected: it changed a directed story beat. Keeping the approved actions and shot order; runtime target may remain unresolved.")
        elif invalid_audio_trim:
            notify("assembly", "Duration trim rejected: it changed or removed a measured dialogue shot. Audio timing and words remain protected; runtime target may remain unresolved.")
        else:
            current_shots = _attach_voice_refs([
                measured.get(s["shot_number"], {**originals.get(s["shot_number"], {}), **s}) for s in cine["shots"]
            ], characters, narrator_voice_ref, emit=emit)
            notify("cinematography", "Applied the duration trim while preserving measured dialogue shots.")

        notify("assembly", "Re-sequencing the trimmed shot list...")
        assembly = call_agent(prompts.SHOT_ASSEMBLER, json.dumps(current_shots))
        assembly["total_duration_sec"] = sum(float(s["duration_sec"]) for s in current_shots)
        notify(
            "assembly",
            f"Runtime now {assembly['total_duration_sec']}s (target {target_duration_sec}s)."
            if assembly["total_duration_sec"] <= target_duration_sec * 1.15
            else f"Still {assembly['total_duration_sec']}s after one trim pass; proceeding with best version.",
        )
    else:
        notify("assembly", f"{actual}s is within range of the {target_duration_sec}s target — no trim needed.")

    ad_direction.accept_shots(current_shots)
    return {"shots": current_shots, "assembly": assembly}


def _prepare_media_parallel(db, job_id, result, *, brief, emit):
    """Private branch snapshots; only this owning thread writes the job or events."""
    from app.services.boundary_continuity import prepare_boundaries
    # Resolve boundary meaning BEFORE either paid preview work or Compiler work starts.
    # Historical retries keep accepted previews; only this review metadata is added.
    prepare_boundaries(result, brief=brief, emit=emit, call_agent=call_agent)
    messages = queue.Queue()
    buffered_events = []
    buffered_keys = {"preview_timing", "still_provider_response",
                     "preview_verification_error", "still_frame"}
    def flush_events():
        if buffered_events:
            job_service.append_events(db, job_id, buffered_events)
            buffered_events.clear()
    started = time.monotonic()
    result["video_prompts_pending"] = True
    result.pop("video_prompt_error", None)
    # Only edited shots lose their approved instructions; siblings remain reusable.
    edited = set(result.get("plan_edited_shots", []))
    for shot in result["shots"]:
        if not edited or shot["shot_number"] in edited:
            shot.pop("compiled_prompt", None)
    job_service.set_result(db, job_id, result)

    def worker(kind, snapshot):
        notify = lambda key, note: messages.put(("event", key, note))
        notify("media_preparation_timing", json.dumps({"branch": kind, "phase": "started",
            "elapsed_sec": round(time.monotonic() - started, 3)}))
        try:
            if kind == "previews":
                # A completed early-preview branch may already have accepted
                # most shots while speech was rendering. Retry only missing or
                # explicitly edited images; never reconsider paid accepted work.
                preview_targets = edited or {shot["shot_number"] for shot in snapshot["shots"]
                                              if not shot.get("still_frame_url")}
                snapshot["shots"] = generate_still_frames(snapshot, job_id=job_id, emit=notify, shot_numbers=preview_targets,
                    on_progress=lambda current: messages.put(("preview_progress", copy.deepcopy(current))))
                messages.put(("preview_progress", snapshot))
            else:
                if edited:
                    snapshot["shots"] = [s for s in snapshot["shots"] if s["shot_number"] in edited]
                shots = compile_shot_prompts(snapshot, brief=brief, emit=notify, call_agent=call_agent,
                    on_checkpoint=lambda checkpoint: messages.put(("compiler_checkpoint", copy.deepcopy(checkpoint))))
                expected = {s["shot_number"] for s in snapshot["shots"]}
                if len(shots) != len(expected) or {s["shot_number"] for s in shots} != expected or any(
                        not isinstance(s.get("compiled_prompt"), str) or not s["compiled_prompt"].strip() for s in shots):
                    raise ValueError("Compiler returned incomplete shot prompts")
                messages.put(("compiled", shots))
        except Exception as error:
            messages.put(("failed", kind, error))
        finally:
            messages.put(("finished", kind, time.monotonic() - started))

    for kind in ("previews", "compiler"):
        threading.Thread(target=worker, args=(kind, copy.deepcopy(result)),
                         daemon=True, name=f"prepare-{kind}").start()
    remaining = {"previews", "compiler"}
    while remaining:
        message = messages.get()
        action = message[0]
        if action == "event":
            if message[1] in buffered_keys:
                buffered_events.append((message[1], message[2]))
            else:
                emit(message[1], message[2])
        elif action == "preview_progress":
            flush_events()
            current = message[1]
            by_number = {s["shot_number"]: s for s in current["shots"]}
            for shot in result["shots"]:
                rendered = by_number[shot["shot_number"]]
                # The preview snapshot may predate Compiler completion. It owns
                # only these fields, never prompt/audio/timing/transition data.
                for key in list(shot):
                    if key.startswith("still_frame_") or key in {"preview_input", "preview_dependencies"}:
                        shot.pop(key)
                shot.update({k: v for k, v in rendered.items()
                    if k.startswith("still_frame_") or k in {"preview_input", "preview_dependencies"}})
            result["entity_references"] = current.get("entity_references", {})
            job_service.set_result(db, job_id, result)
        elif action == "compiler_checkpoint":
            result["video_prompt_checkpoint"] = message[1]
            job_service.set_result(db, job_id, result)
        elif action == "compiled":
            result.pop("video_prompt_checkpoint", None)
            compiled = {s["shot_number"]: s["compiled_prompt"] for s in message[1]}
            for shot in result["shots"]:
                if shot["shot_number"] in compiled:
                    shot["compiled_prompt"] = compiled[shot["shot_number"]]
            result["video_prompts_pending"] = False
            job_service.set_result(db, job_id, result)
        elif action == "failed":
            flush_events()
            kind, error = message[1:]
            if kind == "compiler":
                result["video_prompts_pending"] = False
                result["video_prompt_error"] = "Video instructions could not be prepared. Your accepted previews are saved; retry preparation."
                emit("shot_prompt_compiler", f"Shot Prompt Compiler failed: {type(error).__name__}: {error}")
                job_service.set_status(db, job_id, "error", error_message=f"Shot Prompt Compiler failed: {error}")
            else:
                for shot in result["shots"]:
                    if not shot.get("still_frame_url"):
                        shot.update(still_frame_status="failed", still_frame_warning="Preview preparation failed; retry this preview.")
                emit("still_frame", f"WARNING: Preview preparation failed: {type(error).__name__}; accepted previews preserved.")
            job_service.set_result(db, job_id, result)
        elif action == "finished":
            flush_events()
            remaining.remove(message[1])
            emit("media_preparation_timing", json.dumps({"branch": message[1], "phase": "finished",
                "elapsed_sec": round(message[2], 3)}))
    flush_events()
    if not result.get("video_prompt_error") and all(s.get("still_frame_url") for s in result["shots"]):
        result.pop("plan_edited_shots", None)
        job_service.set_result(db, job_id, result)


@checkpointed_planning
def finalize_audio_assembly(db, job_id):
    """Persist final assembly after all selected audio tasks have corrected durations."""
    job = job_service.get_job(db, job_id)
    result = job_service.job_result(job)
    result["preview_preparation_pending"] = True
    job_service.set_result(db, job_id, result)
    def emit(key, note):
        job_service.append_event(db, job_id, key, note)
    try:
        if any(s.get("has_dialogue") and s.get("status") != "done" for s in result["shots"]):
            raise ValueError("Dialogue audio incomplete; final assembly cannot lock timing")
        emit("assembly", "Speech timing is ready; updating the shot timeline.")
        continuity = result.get("continuity", {})
        old_assembly = result.get("assembly", {})
        duration = sum(float(s.get("duration_sec", 0)) for s in result["shots"])
        if not old_assembly.get("provisional", True) and abs(float(old_assembly.get("total_duration_sec", -1)) - duration) < .001:
            assembled = {"shots": result["shots"], "assembly": old_assembly}
            emit("assembly", "Unchanged accepted transitions and measured duration restored.")
        else:
            assembled = assemble_shots(result["shots"], continuity.get("characters", []), result["format"]["duration_target_sec"],
                                       narrator_voice_ref=continuity.get("narrator_voice_ref"), emit=emit)
        result.update(assembled)
        result["assembly"]["provisional"] = False
    except Exception as error:
        result["assembly"] = {**result.get("assembly", {}), "provisional": True, "error": str(error)}
        emit("assembly", f"Final audio assembly failed: {type(error).__name__}: {error}")
    finally:
        # Dialogue compilation waits for REAL post-audio Assembly transitions.
        # Compiler failure is distinct from Assembly failure; do not mark an
        # already successful audio/assembly pass provisional or discard its data.
        if not result.get("assembly", {}).get("provisional", True):
            try:
                result["ai_model"] = result.get("ai_model") or job.ai_model
                _prepare_media_parallel(db, job_id, result, brief=job.brief, emit=emit)
            except Exception as error:
                result["video_prompts_pending"] = False
                result["video_prompt_error"] = "Video instructions could not be prepared. Your accepted previews are saved; retry preparation."
                emit("shot_prompt_compiler", f"Shot Prompt Compiler failed: {type(error).__name__}: {error}")
                job_service.set_status(db, job_id, "error", error_message=f"Shot Prompt Compiler failed: {error}")
        result["audio_assembly_pending"] = False
        result["preview_preparation_pending"] = False
        job_service.set_result(db, job_id, result)


@checkpointed_planning
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
        direction = json.loads(job.creative_direction_json) if job.creative_direction_json else None
        from app.services.clarifier_service import planning_direction
        direction_note = ("\nUser-reviewed production direction (not spoken dialogue; preserve source-script words):\n"
            + json.dumps(planning_direction(direction), ensure_ascii=False)) if direction else ""
        commercial_context = {"ad_type": job.ad_type, "ad_brief": json.loads(job.ad_brief_json or "{}")}
        direction_note += "\nCommercial format instructions:\n" + prompts.COMMERCIAL_DIRECTIONS[job.ad_type]
        direction_note += "\nUser commercial settings (preserve explicit script; do not invent claims):\n" + json.dumps(commercial_context, ensure_ascii=False)
        polish_note = prompt_polish_context(
            job.ad_type,
            brief=brief,
            language=language,
            duration_seconds=None,
            aspect_ratio=job.aspect_ratio or "16:9",
            visual_style=job.visual_style,
            color_grade=job.color_grade,
            quality=job.quality,
            video_model=job.video_model or job.ai_model,
        )
        if direction:
            emit("clarifier_handoff", "User-reviewed production direction and answers supplied to planning.")

        # 1. Format Classifier — cheap/fast model, this step is pure classification.
        emit("format", "Reading the request, choosing format and structure...")
        fmt = call_agent(prompts.FORMAT_CLASSIFIER, brief + direction_note, fast=True)
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
                f"Named locations: {json.dumps(list(resolved_names.get('locations', {})), ensure_ascii=False)}" + direction_note,
            )
        else:
            script = call_agent(
                prompts.SCRIPT_ARCHITECT % language,
                f"Brief: {brief}\nFormat: {fmt['format']}\nStructure: {fmt['structure']}\nNumber of scenes: {fmt['num_scenes']}" + direction_note,
            )
        emit("script", f"Logline locked: \"{script['logline']}\"")
        # Preserve authoritative context with the saved story for initial QA AND
        # later edit/retry paths. No separate requirement-extraction model call.
        from app.services.product_service import job_references
        script['production_context'] = {
            'commercial': commercial_context,
            'original_brief': brief, 'source_script': source_script,
            'reviewed_direction': planning_direction(direction),
            'products': [{'name': p['name'], 'approved_views': [{'angle': v['angle'], 'provenance': v['provenance']} for v in p.get('views', [])]} for p in job_references(db, job_id)],
        }

        # 3. Visual Continuity Agent — builds the reference library BEFORE any
        # shot is planned, so every later step can be checked against it.
        emit("continuity_plan", "Building the reference asset library before any shot is planned...")
        continuity_input = json.dumps(script["scenes"])
        continuity_input += "\nAuthoritative job style settings: " + json.dumps({
            "visual_style": job.visual_style, "color_grade": job.color_grade,
        })
        if source_script:
            continuity_input += (
                "\nEvery named script entity below must have one matching Continuity entry; preserve each name exactly:\n"
                f"Characters: {json.dumps(list((resolutions or {}).get('characters', {})), ensure_ascii=False)}\n"
                f"Locations: {json.dumps(list((resolutions or {}).get('locations', {})), ensure_ascii=False)}"
            )
        continuity = call_agent(prompts.CONTINUITY_AGENT, continuity_input + direction_note)
        override_stats = _apply_continuity_overrides(db, continuity, resolutions, job_brief=brief, emit=emit)
        assigned_voice_count = voice_generation_service.assign_missing_voice_ids(continuity, emit=emit)
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
        emit("cinematography", "Directing the story: action, performance, product staging, camera and pacing...")
        cinematography_input = (
            f"Scenes: {json.dumps(script['scenes'])}\nCharacters: {json.dumps(creative_references(continuity['characters']))}"
            f"\ncontinuity.visual_style: {json.dumps(continuity['visual_style'])}"
        )
        cinematography_input += f"\nDialogue source: {'user-supplied script; preserve words verbatim' if source_script else 'AI-written scene breakdown; use the selected language and its native script'}. Selected spoken language: {language}."
        cinematography_input += f"\ncamera_options: {json.dumps(camera_options())}"
        if source_script:
            cinematography_input += f"\nLocations: {json.dumps(continuity['locations'])}"
        cinematography_input += f"\nSelected video model: {job.video_model or job.ai_model}. Use plain natural-language camera instructions; no invented provider control tokens."
        if job.video_model == "kling_avatar_fal":
            cinematography_input += "\nExperimental speaking-avatar model: prefer a held viewpoint and restrained performance; complex scene-camera motion is unverified."
        from app.services.dialogue_duration import MIN_SHOT_SECONDS
        minimum_shot_seconds = MIN_SHOT_SECONDS
        if fmt["duration_target_sec"] < minimum_shot_seconds:
            raise ValueError(f"The selected video model requires at least {minimum_shot_seconds} seconds. Increase the requested duration before retrying.")
        if minimum_shot_seconds:
            cinematography_input += f"\nMinimum generated shot duration: {minimum_shot_seconds} seconds. Group compatible sequential actions into complete beats; do not buy many tiny shots. Never merge distinct complete speaking turns or omit story events."
        if fmt["duration_target_sec"] <= 12:
            cinematography_input += f"\nPrefer ONE continuous shot at this short duration if every required story beat, complete dialogue and achievable action fits. Multiple shots are allowed only for necessary location/time changes, incompatible staging or separate speakers. Never omit beats to force one shot. Each shot remains at least {minimum_shot_seconds} seconds; total must fit the target. Explain necessary cuts in edit_intent."
        cinematography_input += f"\nTarget total duration: {fmt['duration_target_sec']} seconds"
        from app.services.voice_timing import measured_budget
        cinematography_input += f"\nMeasured dialogue budget: {json.dumps(measured_budget(db, language))}"
        token_budget = cinematography_token_budget(script["scenes"], fmt["duration_target_sec"])
        script['production_context']['visual_style'] = continuity['visual_style']
        cinematography_input += "\nSource-linked requirements: " + json.dumps(story_requirements.review_context(script))
        cinematography_input += "\nApproved story and format: " + json.dumps({'logline': script['logline'], 'format': fmt['format']})

        def record_cinematography_usage(metadata: dict) -> None:
            if metadata["will_retry"]:
                emit("cinematography", "Shot planning reached its response limit; retrying once with more room.")

        cine = call_agent(
            prompts.CINEMATOGRAPHY_AGENT,
            cinematography_input + direction_note + polish_note,
            max_tokens=token_budget,
            truncation_retry_tokens=min(32768, token_budget * 2),
            on_response=record_cinematography_usage,
        )
        directed_ad = ad_direction.validate_ad(cine.get('ad_direction'))
        for shot in cine['shots']:
            shot['direction_version'] = 1
        from app.services.dialogue_duration import preflight_dialogue_durations
        render_camera_summaries(cine["shots"])
        preflight_dialogue_durations(cine["shots"], emit=emit)
        assigned_voice_count += voice_generation_service.assign_missing_voice_ids(continuity, cine["shots"], emit=emit)
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
        # Display-only snapshot: never put unreviewed shots in result.shots.
        # Running/error jobs cannot be approved; final persistence replaces this.
        job_service.set_result(db, job_id, {'planning_draft': {
            'logline': script.get('logline', ''),
            'target_duration_sec': fmt['duration_target_sec'],
            'characters': [c['name'] for c in continuity['characters']],
            'shots': [{k: s[k] for k in ('shot_number', 'scene_number', 'description',
                'dialogue_text', 'duration_sec') if k in s} for s in cine['shots']],
        }})
        emit('planning_draft', 'Your Director plan is ready. Checking technical compatibility before your review.')
        validated = validate_and_correct(
            cine["shots"],
            continuity["characters"],
            fmt["duration_target_sec"],
            narrator_voice_ref=continuity.get("narrator_voice_ref"),
            source_script_text=source_script,
            emit=emit,
            minimum_shot_seconds=minimum_shot_seconds,
            ad_direction_plan=directed_ad,
            approved_story=script,
            semantic_review=True,
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
            **commercial_context,
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
            "creative_direction": direction,
            "ad_direction": directed_ad,
            "planning_constraints": {"minimum_shot_seconds": minimum_shot_seconds},
            "sound_direction_status": "planned_only",
        }
        if source_script:
            result["source_script_text"] = source_script
        # Both silent and dialogue jobs stop at the written plan. Paid previews
        # start only after the same explicit approval action.
        job_service.set_result(db, job_id, result)
        job_service.set_status(db, job_id, "done")

    except Exception as exc:  # noqa: BLE001 — surface any failure to the job record
        saved = job_service.job_result(job_service.get_job(db, job_id))
        if saved and saved.get("video_prompts_pending"):
            saved["video_prompts_pending"] = False
            saved["video_prompt_error"] = "Video instructions could not be prepared. Your accepted previews are saved; retry preparation."
            job_service.set_result(db, job_id, saved)
        job_service.set_status(db, job_id, "error", error_message=str(exc))
        emit("error", f"Pipeline failed: {exc}")
