"""Shared creative contract, not another agent or generation pipeline.

The existing Cinematography call authors direction; code validates and projects
it. Legacy plans remain usable without fabricating new creative decisions.
"""
import hashlib
import json
import re

_SPEECH_CUE = re.compile(r'\b(says?|speaks?|asks?|answers?|replies?|whispers?|shouts?|calls?|utters?|delivers?|announces?|narrates?|voice[- ]?over|line)\b', re.I)
_DEFERRED_SPEECH = re.compile(r'\b(?:only\s+)?after\b|\bbefore\b.+\b(?:speaks?|says?|line)\b', re.I)

AD_FIELDS = ("takeaway", "visual_approach", "pacing", "sound_direction")
SHOT_FIELDS = ("purpose", "performance", "product_props", "edit_intent")
# These are authored by the Director and copied verbatim into still/video
# contracts.  They are facts, not prose for a renderer to infer.
EXECUTION_FIELDS = ("blocking", "action_beats", "critical_outcome", "entry_exit_paths",
                    "support_and_contact", "spatial_invariants", "forbidden_geometry")
DIRECTION_FIELDS = ("shot_direction", "state_at_shot_start", "state_at_shot_end", "opening_characters")


def opening_cast(shot):
    return shot.get('opening_characters', []) if shot.get('direction_version') == 1 else shot.get('characters_in_shot', [])


def shot_visual_text(result, shot, text, *, opening=False):
    """Drop project-wide appearance clauses about subjects absent from this shot."""
    from app.services.video_references import mentions

    cast = opening_cast(shot) if opening else (shot.get('characters_in_shot') or [])
    visible = {name.casefold() for name in cast}
    absent = [character.get('name', '') for character in
              (result.get('continuity') or {}).get('characters', [])
              if character.get('name') and character['name'].casefold() not in visible]
    def keep(clause):
        return (not any(mentions(clause, name) for name in absent)
                and (visible or not re.search(
                    r'\b(?:skin tones?|lifelike skin|wardrobe|people|person|human|faces?|hair|divine elements)\b',
                    clause, re.I)))
    return ', '.join(part.strip() for part in re.split(r'[,;]\s*', text)
                     if part.strip() and keep(part))


def shot_visual_style(result, shot, *, opening=False):
    """Keep the project look without introducing subjects absent from this frame/clip."""
    style = (result.get('continuity') or {}).get('visual_style') or result.get('visual_style')
    if isinstance(style, dict):
        return {key: (shot_visual_text(result, shot, value, opening=opening)
                      if isinstance(value, str) else value)
                for key, value in style.items()}
    if isinstance(style, str):
        return shot_visual_text(result, shot, style, opening=opening)
    return style


def validate_ad(value):
    if not isinstance(value, dict) or set(value) != set(AD_FIELDS):
        raise ValueError("Ad direction is incomplete. Retry planning; no media has been generated.")
    if any(not isinstance(value[k], str) or not value[k].strip() or len(value[k]) > 500 for k in AD_FIELDS):
        raise ValueError("Ad direction requires concise, nonempty instructions for every field.")
    return value


def source_key(shot):
    # A user text edit must not reuse the previous action's opening/performance.
    return hashlib.sha256(json.dumps([shot.get(k) for k in
        ("description", "dialogue_text", "characters_in_shot", "speech_mode")],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def problems(shot):
    if shot.get("direction_version") != 1:
        return []
    found = []
    cast = shot.get('opening_characters')
    if (not isinstance(cast, list) or any(not isinstance(n, str) or n not in shot.get('characters_in_shot', []) for n in cast)
            or len(cast) != len(set(cast))):
        found.append('opening_characters must list only the exact cast names visible at the opening, not later arrivals')
    direction = shot.get("shot_direction")
    if (not isinstance(direction, dict) or not set(SHOT_FIELDS).issubset(direction)
            or set(direction) - set(SHOT_FIELDS) - set(EXECUTION_FIELDS) - {'dialogue_beat_index'}
            or any(not isinstance(direction.get(k), str) or not direction[k].strip()
                   or len(direction[k]) > 350 for k in SHOT_FIELDS)):
        found.append("shot_direction needs concise purpose, performance, product_props and edit_intent")
    if isinstance(direction, dict) and any(k in direction for k in EXECUTION_FIELDS):
        for key in ('blocking', 'critical_outcome'):
            if not isinstance(direction.get(key), str) or not direction[key].strip() or len(direction[key]) > 500:
                found.append(f"shot_direction.{key} needs one concrete, concise visual instruction")
        beats = direction.get('action_beats')
        if (not isinstance(beats, list) or not 2 <= len(beats) <= 3
                or any(not isinstance(b, str) or not b.strip() or len(b) > 350 for b in beats)):
            found.append('shot_direction.action_beats needs two or three ordered, achievable visual beats')
        # The structured Director contract requires this on new output. Keep
        # older saved plans usable; their unique explicit speech cue can be
        # resolved deterministically by dialogue_window during regeneration.
        if 'dialogue_beat_index' in direction:
            dialogue_index = direction.get('dialogue_beat_index')
            speaks = shot.get('speech_mode') in ('onscreen', 'voiceover') or bool(shot.get('has_dialogue'))
            if (not isinstance(dialogue_index, int) or isinstance(dialogue_index, bool)
                    or (speaks and isinstance(beats, list) and not 1 <= dialogue_index <= len(beats))
                    or (not speaks and dialogue_index != 0)):
                found.append('shot_direction.dialogue_beat_index must identify the speaking action beat, or be 0 for silence')
            elif speaks and isinstance(beats, list):
                speaking_beat = beats[dialogue_index - 1]
                if not _SPEECH_CUE.search(speaking_beat):
                    found.append('the dialogue beat must explicitly describe delivery of the approved line')
                if _DEFERRED_SPEECH.search(speaking_beat):
                    found.append('actions required before speech need their own earlier action beat; the dialogue beat is delivery only')
                other_speech = [beat for i, beat in enumerate(beats) if i != dialogue_index - 1 and _SPEECH_CUE.search(beat)]
                if other_speech:
                    found.append('only the selected dialogue beat may direct speech')
        spatial = ('entry_exit_paths', 'support_and_contact', 'spatial_invariants', 'forbidden_geometry')
        # Older reviewed plans remain editable/regenerable. Fresh Director-v2
        # output is schema-required to provide the complete spatial contract.
        if any(key in direction for key in spatial):
            if not isinstance(direction.get('support_and_contact'), str) or not direction['support_and_contact'].strip() or len(direction['support_and_contact']) > 500:
                found.append('shot_direction.support_and_contact needs one concrete, concise visual instruction')
            for key in ('entry_exit_paths', 'spatial_invariants', 'forbidden_geometry'):
                values = direction.get(key)
                if (not isinstance(values, list) or not 1 <= len(values) <= 5
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 350 for value in values)):
                    found.append(f'shot_direction.{key} needs one to five explicit physical facts')
    for field in ("state_at_shot_start", "state_at_shot_end"):
        if not isinstance(shot.get(field), str) or not shot[field].strip() or len(shot[field]) > 500:
            found.append(f"{field} must describe one visible instant, including static shots")
    if shot.get("direction_source") and shot["direction_source"] != source_key(shot):
        found.append("The action or dialogue was edited; refresh opening/end states and performance to match it")
    return found


def execution_sections(shot):
    """Project approved decisions verbatim; never invent action during compilation."""
    direction = shot.get('shot_direction') or {}
    sections = []
    if direction.get('blocking'):
        sections.append(('Staging', direction['blocking']))
    if direction.get('entry_exit_paths'):
        sections.append(('Movement paths', ' '.join(direction['entry_exit_paths'])))
    if direction.get('support_and_contact'):
        sections.append(('Physical support and contact', direction['support_and_contact']))
    if direction.get('spatial_invariants'):
        sections.append(('Spatial facts that must remain true', ' '.join(direction['spatial_invariants'])))
    if direction.get('forbidden_geometry'):
        sections.append(('Forbidden staging', ' '.join(direction['forbidden_geometry'])))
    if direction.get('action_beats'):
        sections.append(('Action progression', ' Then '.join(
            f'{i + 1}) {text.strip()}' for i, text in enumerate(direction['action_beats']))))
    if direction.get('critical_outcome'):
        sections.append(('Must-see outcome', direction['critical_outcome']))
    return sections


def check_plan(qa, shots):
    issues = []
    for shot in shots:
        errors = problems(shot)
        if errors:
            issues.append({"shot_number": shot["shot_number"], "code": "ad_direction_contract",
                "problem": "; ".join(errors), "fix_instruction":
                "Repair only shot_direction, opening_characters and state_at_shot_start/end for the current action; preserve all dialogue and other fields."})
    # Collect literal duplicates in one pass rather than making the reasoning
    # model discover one on each retry. Semantic near-duplicates remain with QA.
    normalize = lambda value: re.sub(r'[-\s]+', ' ', str(value or '').casefold()).strip()
    for left, right in zip(shots, shots[1:]):
        framing = normalize(left.get('camera_angle'))
        if right.get('direction_version') == 1 and framing and framing == normalize(right.get('camera_angle')):
            issues.append({'shot_number': right['shot_number'], 'code': 'duplicate_directed_framing',
                'problem': f"Camera angle/framing repeats shot {left['shot_number']} exactly",
                'fix_instruction': 'Change only camera_angle to a motivated distinct viewpoint/scale; retain the action and staging.'})
    return {**qa, "approved": False, "issues": [*qa.get("issues", []), *issues]} if issues else qa


def accept_shots(shots, shot_numbers=None):
    targets = set(shot_numbers) if shot_numbers is not None else None
    for shot in shots:
        if targets is not None and shot.get('shot_number') not in targets:
            continue
        if shot.get("direction_version") == 1:
            errors = problems(shot)
            if errors:
                raise ValueError(f"Shot {shot['shot_number']} direction is not ready: {'; '.join(errors)}")
            shot["direction_source"] = source_key(shot)


def check_coverage(qa, shots, story):
    """Validate review evidence, not the meaning of a story. Meaning stays QA's job."""
    scenes = [s for s in (story or {}).get('scenes', [])
              if any(s.get(k) for k in ('heading', 'description', 'dialogue_or_vo'))]
    if not scenes:
        return qa
    expected = {s['scene_number'] for s in scenes}
    rows = qa.get('scene_coverage')
    by_shot = {s['shot_number']: s for s in shots}
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(expected)
            or {r.get('scene_number') for r in rows} != expected):
        raise ValueError('Story review omitted scene coverage. Your plan is saved; retry planning.')
    issues = list(qa.get('issues', []))
    for row in rows:
        numbers = row.get('shot_numbers')
        if (not isinstance(row.get('covered'), bool) or not isinstance(numbers, list)
                or any(not isinstance(n, int) or isinstance(n, bool) or n not in by_shot
                       or by_shot[n].get('scene_number') != row['scene_number'] for n in numbers)
                or len(numbers) != len(set(numbers))
                or (row['covered'] and not numbers)
                or not isinstance(row.get('evidence'), str) or not row['evidence'].strip()):
            raise ValueError('Story review returned invalid coverage evidence. Retry planning.')
        if not row['covered']:
            if any(i.get('shot_number') in by_shot and
                   by_shot[i['shot_number']].get('scene_number') == row['scene_number'] for i in issues):
                continue  # Preserve QA's actual target/scope; do not invent a duplicate.
            number = next((s['shot_number'] for s in shots if s.get('scene_number') == row['scene_number']), shots[0]['shot_number'])
            issues.append({'shot_number': number, 'code':'story_coverage',
                'problem': f"Scene {row['scene_number']} missing story execution: {row['evidence']}",
                'fix_instruction':'Restore the missing approved scene beats; preserve all dialogue and unaffected choices.'})
    return {**qa, 'approved': bool(qa.get('approved')) and not issues, 'issues':issues}


def visual_direction(result):
    value = result.get("ad_direction")
    if not value:
        return None
    validate_ad(value)
    # Music is a future edit intention, never an instruction to draw or generate
    # unlicensed music inside a shot. Sound execution is explicitly separate.
    return {k: value[k] for k in ("takeaway", "visual_approach", "pacing")}
