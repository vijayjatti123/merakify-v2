"""Stable provider schemas; semantic correctness stays with the existing QA loop.

No user data enters schema definitions. Keep these small enough for grammar reuse.
Legacy full-plan repair retains its richer metadata and existing validation.
"""
import copy
import json
import math


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}


def array(items):
    return {'type': 'array', 'items': items}


TEXT = {'type': 'string'}
INTEGER = {'type': 'integer'}
NUMBER = {'type': 'number'}
BOOLEAN = {'type': 'boolean'}


def contracts():
    from app.services import camera_direction as camera
    from app.services.ad_direction import AD_FIELDS, SHOT_FIELDS
    cam = obj({key: {'type': 'string', 'enum': sorted(values)} for key, values in (
        ('movement', camera.MOVEMENTS), ('direction', camera.DIRECTIONS),
        ('speed', camera.SPEEDS), ('stabilization', camera.STABILIZATIONS))})
    direction = obj({k: TEXT for k in SHOT_FIELDS})
    visual = {k: TEXT for k in ('camera_angle', 'lens', 'lighting', 'composition_note',
                               'description', 'state_at_shot_start', 'state_at_shot_end')}
    visual.update(camera_direction=cam, duration_sec=NUMBER, opening_characters=array(TEXT), shot_direction=direction)
    shot = obj(dict(shot_number=INTEGER, scene_number=INTEGER, **visual,
                    characters_in_shot=array(TEXT), has_dialogue=BOOLEAN,
                    speech_mode={'type': 'string', 'enum': ['none', 'onscreen', 'voiceover']}, dialogue_text=TEXT))
    coverage = obj(dict(scene_number=INTEGER, shot_numbers=array(INTEGER), covered=BOOLEAN, evidence=TEXT))
    issue = obj(dict(shot_number=INTEGER, problem=TEXT, fix_instruction=TEXT))
    return {
        'director-v1': obj(dict(ad_direction=obj({k: TEXT for k in AD_FIELDS}), shots=array(shot))),
        'qa-v1': obj(dict(approved=BOOLEAN, scene_coverage=array(coverage), issues=array(issue))),
        'patch-v1': obj(dict(patches=array(obj(dict(shot_number=INTEGER, changes=obj(visual, [])))))),
    }


def contract_for(system, content):
    from app.agents import prompts
    from app.config import settings
    if not settings.planning_structured_outputs:
        return None, None
    name = None
    if system == prompts.CINEMATOGRAPHY_AGENT:
        name = 'director-v1'
    elif system == prompts.CINEMATOGRAPHY_PATCH:
        name = 'patch-v1'
    elif system == prompts.QA_AGENT:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get('ad_direction'):
            name = 'qa-v1'
    return (name, copy.deepcopy(contracts()[name])) if name else (None, None)


def validate(value, schema, path='response'):
    """Validate this module's small schema vocabulary before checkpointing.

    Defense against refusals, partial output and unsupported provider behavior;
    does not replace numeric bounds, camera compatibility or semantic checks.
    """
    kind = schema['type']
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str), 'boolean': lambda: isinstance(value, bool),
             'integer': lambda: isinstance(value, int) and not isinstance(value, bool),
             'number': lambda: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}[kind]()
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise ValueError(f'Planning response violates its structured contract at {path}. Please retry.')
    if kind == 'object':
        if not set(schema['required']) <= value.keys() or not value.keys() <= schema['properties'].keys():
            raise ValueError(f'Planning response has missing or unexpected fields at {path}. Please retry.')
        for key, child in value.items():
            validate(child, schema['properties'][key], f'{path}.{key}')
    elif kind == 'array':
        for index, child in enumerate(value):
            validate(child, schema['items'], f'{path}[{index}]')
