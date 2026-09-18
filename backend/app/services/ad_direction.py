"""Shared creative contract, not another agent or generation pipeline.

The existing Cinematography call authors direction; code validates and projects
it. Legacy plans remain usable without fabricating new creative decisions.
"""
import hashlib
import json
import re

AD_FIELDS = ("takeaway", "visual_approach", "pacing", "sound_direction")
SHOT_FIELDS = ("purpose", "performance", "product_props", "edit_intent")
EXECUTION_FIELDS = ("blocking", "action_beats", "critical_outcome")
DIRECTION_FIELDS = ("shot_direction", "state_at_shot_start", "state_at_shot_end", "opening_characters")


def opening_cast(shot):
    return shot.get('opening_characters', []) if shot.get('direction_version') == 1 else shot.get('characters_in_shot', [])


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
            or set(direction) - set(SHOT_FIELDS) - set(EXECUTION_FIELDS)
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


def accept_shots(shots):
    for shot in shots:
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
