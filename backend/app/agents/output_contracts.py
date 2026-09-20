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
    direction = obj({**{k: TEXT for k in SHOT_FIELDS}, 'blocking': TEXT,
                     'action_beats': array(TEXT), 'critical_outcome': TEXT,
                     'entry_exit_paths': array(TEXT), 'support_and_contact': TEXT,
                     'spatial_invariants': array(TEXT), 'forbidden_geometry': array(TEXT)})
    visual = {k: TEXT for k in ('camera_angle', 'lens', 'lighting', 'composition_note',
                               'description', 'state_at_shot_start', 'state_at_shot_end')}
    visual.update(camera_direction=cam, duration_sec=NUMBER, opening_characters=array(TEXT), shot_direction=direction)
    shot = obj(dict(shot_number=INTEGER, scene_number=INTEGER, **visual,
                    characters_in_shot=array(TEXT), has_dialogue=BOOLEAN,
                    speech_mode={'type': 'string', 'enum': ['none', 'onscreen', 'voiceover']}, speaker_name=TEXT, transition_after={'type':'string','enum':['cut','crossfade','match cut']}, dialogue_text=TEXT))
    coverage = obj(dict(scene_number=INTEGER, shot_numbers=array(INTEGER), covered=BOOLEAN, evidence=TEXT))
    issue = obj(dict(shot_number=INTEGER, problem=TEXT, fix_instruction=TEXT))
    scoped_issue = obj(dict(shot_number=INTEGER, problem=TEXT, fix_instruction=TEXT,
        requirement_id=TEXT, repair_kind={'type':'string', 'enum':['visual_fields', 'insert_after', 'structural']},
        repair_fields=array(TEXT)))
    requirement = obj(dict(requirement_id=TEXT, shot_numbers=array(INTEGER), covered=BOOLEAN, evidence=TEXT))
    directed_shot = copy.deepcopy(shot)
    directed_shot['properties']['shot_direction']['required'] = list(direction['properties'])
    return {
        'director-v1': obj(dict(ad_direction=obj({k: TEXT for k in AD_FIELDS}), shots=array(shot))),
        'director-v2': obj(dict(ad_direction=obj({k: TEXT for k in AD_FIELDS}), shots=array(directed_shot))),
        'qa-v1': obj(dict(approved=BOOLEAN, scene_coverage=array(coverage), issues=array(issue))),
        'qa-v2': obj(dict(approved=BOOLEAN, scene_coverage=array(coverage),
                          requirement_coverage=array(requirement),
                          shot_checks=array(obj(dict(shot_number=INTEGER, consistent=BOOLEAN, evidence=TEXT))),
                          issues=array(scoped_issue))),
        'patch-v1': obj(dict(patches=array(obj(dict(shot_number=INTEGER, changes=obj(visual, [])))))),
        'patch-insert-v1': obj(dict(patches=array(obj(dict(shot_number=INTEGER, changes=obj(visual, [])))),
            insertions=array(obj(dict(after_shot_number=INTEGER,
                shot=obj({k:v for k,v in shot['properties'].items() if k != 'shot_number'})))))),
    }


def contract_for(system, content):
    from app.agents import prompts
    from app.config import settings
    if system.startswith(prompts.CLARIFIER_REFINE):
        payload = json.loads(content)
        script_mode = payload.get('gathered', {}).get('_context', {}).get('input_mode') == 'script'
        fields = ('audience', 'takeaway', 'product_role', 'execution', 'must_haves', 'exclusions', 'open_questions')
        return ('refinement-v1', obj({'production_direction': obj({k: TEXT for k in fields})})
                if script_mode else obj({'refined_prompt': TEXT}))
    try:
        payload = json.loads(content) if system == prompts.QA_AGENT else None
    except (ValueError, TypeError):
        payload = None
    # Required evidence must use the provider's schema, even when the optional
    # Director schema optimization is off. A prose hint alone omitted the matrix
    # in the first live audit; local semantic/reference checks remain mandatory.
    insert_contract = system == prompts.CINEMATOGRAPHY_PATCH and '\nallowed_insert_after:' in content
    required_evidence = (isinstance(payload, dict) and bool(payload.get('requirements'))) or insert_contract
    required_director = system in (prompts.CINEMATOGRAPHY_AGENT, prompts.CINEMATOGRAPHY_PATCH)
    if not settings.planning_structured_outputs and not required_evidence and not required_director:
        return None, None
    name = None
    if system == prompts.CINEMATOGRAPHY_AGENT:
        name = 'director-v2'
    elif system == prompts.CINEMATOGRAPHY_PATCH:
        name = 'patch-insert-v1' if insert_contract else 'patch-v1'
    elif system == prompts.QA_AGENT:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and payload.get('ad_direction'):
            name = 'qa-v2' if payload.get('requirements') else 'qa-v1'
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
