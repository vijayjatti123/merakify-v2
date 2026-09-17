"""Small visual repairs within the existing QA loop; never infer dialogue edits."""
import copy
import re


def patch_permissions(shots, issues):
    by_number = {s['shot_number']: s for s in shots}
    allowed = {}
    for issue in issues:
        number = issue.get('shot_number')
        if number not in by_number:
            return None
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
            elif field == 'duration_sec':
                import math
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Planning correction returned an invalid duration.')
            elif not isinstance(value, str) or not value.strip():
                raise ValueError('Planning correction returned an empty visual instruction.')
        by_number[number].update(changes)
        seen.add(number)
    if seen != set(permissions):
        raise ValueError('Planning correction omitted a flagged shot. Please retry planning.')
    return updated
