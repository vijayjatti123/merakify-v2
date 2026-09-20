"""Small visual repairs within the existing QA loop; never infer dialogue edits."""
import copy
import re

VISUAL_REPAIR_FIELDS = frozenset(('description', 'shot_direction', 'state_at_shot_start',
    'state_at_shot_end', 'opening_characters', 'camera_angle', 'camera_direction',
    'lens', 'lighting', 'composition_note', 'duration_sec'))


def protect_unflagged(shots, revised, issues):
    """Legacy structural output may add shots, but cannot rewrite its neighbors."""
    allowed = {i.get('shot_number') for i in issues}
    by_number = {s['shot_number']: s for s in revised}
    if len(by_number) != len(revised):
        raise ValueError('Correction duplicated shot identifiers. Retry planning.')
    for shot in shots:
        if shot['shot_number'] not in allowed:
            candidate = by_number.get(shot['shot_number'])
            if candidate is None or any(shot.get(k) != v for k, v in candidate.items()):
                raise ValueError('Correction changed an unflagged shot. Your plan is saved; retry planning.')
            # Keep the authoritative snapshot, including metadata the model
            # need not repeat. Omitted fields cannot erase existing data.
            snapshot = copy.deepcopy(shot)
            candidate.clear()
            candidate.update(snapshot)
    original_order = [s['shot_number'] for s in shots if s['shot_number'] not in allowed]
    if [s['shot_number'] for s in revised if s['shot_number'] in original_order] != original_order:
        raise ValueError('Correction reordered unflagged shots. Retry planning.')


def apply_insertion_response(shots, response, permissions, anchors):
    """Insert silent story beats, retaining existing content and mapping ordinals.

    Speech creation/removal is deliberately excluded. The same independent QA
    validates feasibility, boundaries and coverage after this operation.
    """
    from app.agents.output_contracts import contracts, validate
    validate(response, contracts()['patch-insert-v1'])
    updated = apply_patch_response(shots, {'patches': response['patches']}, permissions) if permissions else copy.deepcopy(shots)
    if not permissions and response['patches']:
        raise ValueError('Insertion response changed unauthorized existing shots.')
    by_number = {s['shot_number']: s for s in shots}
    insertions = {}
    for operation in response['insertions']:
        anchor, shot = operation['after_shot_number'], copy.deepcopy(operation['shot'])
        if anchor not in anchors or anchor in insertions:
            raise ValueError('Insertion targeted an unauthorized or duplicate boundary.')
        if shot['has_dialogue'] or shot['dialogue_text'] or shot['speech_mode'] != 'none':
            raise ValueError('A visual beat insertion cannot invent speech.')
        if shot['scene_number'] != by_number[anchor]['scene_number']:
            raise ValueError('Insertion changed the approved scene.')
        shot['direction_version'] = 1
        insertions[anchor] = shot
    if set(insertions) != set(anchors):
        raise ValueError('Correction omitted a required beat insertion.')
    result, number_map = [], {}
    for shot in updated:
        old = shot['shot_number']
        shot['shot_number'] = len(result) + 1
        number_map[old] = shot['shot_number']
        result.append(shot)
        if old in insertions:
            result.append({**insertions[old], 'shot_number': len(result) + 1})
    return result, number_map


def patch_permissions(shots, issues):
    by_number = {s['shot_number']: s for s in shots}
    allowed = {}
    for issue in issues:
        number = issue.get('shot_number')
        if number not in by_number:
            return None
        # Explicit semantic scopes are restricted by code, never arbitrary keys
        # supplied by the model. Speech/cast/scene identity cannot be patched.
        if issue.get('repair_kind') == 'visual_fields':
            fields = issue.get('repair_fields')
            if not isinstance(fields, list) or not fields or any(f not in VISUAL_REPAIR_FIELDS for f in fields):
                raise ValueError('QA proposed an invalid visual repair scope. Retry planning.')
            fields = set(fields)
            if 'description' in fields:
                fields.update(('shot_direction', 'state_at_shot_start', 'state_at_shot_end', 'opening_characters'))
            allowed.setdefault(number, set()).update(fields)
            continue
        if issue.get('code') == 'invalid_camera_direction':
            allowed.setdefault(number, set()).add('camera_direction')
            continue
        if issue.get('code') == 'duration_bounds':
            allowed.setdefault(number, set()).add('duration_sec')
            continue
        if issue.get('code') == 'ad_direction_contract':
            from app.services.ad_direction import DIRECTION_FIELDS
            allowed.setdefault(number, set()).update(DIRECTION_FIELDS)
            continue
        text = (str(issue.get('problem', '')) + ' ' + str(issue.get('fix_instruction', ''))).lower()
        # Relationship/structural repairs need the established complete-plan path.
        if re.search(r'dialogue|utterance|speaker|split|merge|boundary|state_|180|eyeline|axis|screen.direction|delete|remove', text):
            return None
        fields = set()
        if re.search(r'camera.angle|framing|shot.scale|scale/angle|close.up|wide shot', text):
            fields.add('camera_angle')
        if re.search(r'camera direction|camera_direction|camera.movement|stabilization|locked.off|moving camera', text):
            fields.add('camera_direction')
        if re.search(r'\blens\b|\bfocus\b', text):
            fields.add('lens')
        if re.search(r'\blighting\b', text):
            fields.add('lighting')
        if re.search(r'\bduration\b|duration_sec|9.second.*cap', text):
            fields.add('duration_sec')
        if not fields:
            return None
        allowed.setdefault(number, set()).update(fields)
    return {number: sorted(fields) for number, fields in allowed.items()} or None


def apply_patch_response(shots, response, permissions):
    patches = response.get('patches') if isinstance(response, dict) else None
    if not isinstance(patches, list) or not patches:
        raise ValueError('Planning correction returned no field patches. Please retry planning.')
    updated = copy.deepcopy(shots)
    by_number = {s['shot_number']: s for s in updated}
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {'shot_number', 'changes'}:
            raise ValueError('Planning correction returned an invalid field patch. Please retry planning.')
        number, changes = patch['shot_number'], patch['changes']
        if isinstance(number, bool) or not isinstance(number, int) or number not in permissions or number in seen:
            raise ValueError('Planning correction targeted an unauthorized shot. Please retry planning.')
        if not isinstance(changes, dict) or not changes or not set(changes) <= set(permissions[number]):
            raise ValueError('Planning correction changed an unauthorized field. Please retry planning.')
        for field, value in changes.items():
            if field == 'camera_direction':
                from app.services.camera_direction import validate
                validate(value)
            elif field == 'opening_characters':
                if not isinstance(value, list) or any(not isinstance(n, str) or n not in by_number[number].get('characters_in_shot', []) for n in value):
                    raise ValueError('Opening cast must be drawn from the existing shot cast.')
            elif field == 'shot_direction':
                from app.services.ad_direction import SHOT_FIELDS, EXECUTION_FIELDS
                if (not isinstance(value, dict) or set(value) != set(SHOT_FIELDS) | set(EXECUTION_FIELDS)):
                    raise ValueError('Planning correction returned incomplete shot direction.')
                probe = {**by_number[number], 'shot_direction': value, 'direction_version': 1}
                from app.services.ad_direction import problems
                if problems(probe):
                    raise ValueError('Planning correction returned incomplete physical staging.')
            elif field == 'duration_sec':
                import math
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Planning correction returned an invalid duration.')
            elif not isinstance(value, str) or not value.strip():
                raise ValueError('Planning correction returned an empty visual instruction.')
        by_number[number].update(changes)
        from app.services.ad_direction import DIRECTION_FIELDS, source_key
        if set(DIRECTION_FIELDS) <= set(changes):
            by_number[number]['direction_source'] = source_key(by_number[number])
        seen.add(number)
    if seen != set(permissions):
        raise ValueError('Planning correction omitted a flagged shot. Please retry planning.')
    return updated
