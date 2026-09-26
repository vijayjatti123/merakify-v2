"""Source-quoted review checklist. No summarization or extra provider call."""
import re


def requirements(story):
    rows = []
    for scene in (story or {}).get('scenes', []):
        number = scene['scene_number']
        for field in ('heading', 'description', 'dialogue_or_vo'):
            text = scene.get(field) or ''
            # Sentence boundaries only: do not guess action counts or paraphrase.
            parts = re.split(r'(?<=[.!?।])\s+', text.strip()) if field == 'description' else [text.strip()]
            for index, quote in enumerate(parts, 1):
                if quote:
                    rows.append({'id': f's{number}:{field}:{index}', 'scene_number': number,
                                 'source_field': field, 'quote': quote})
    return rows


def review_context(story):
    return {'requirements': requirements(story),
            'source_context': (story or {}).get('production_context', {})}


def derive_scene_coverage(verdict, story):
    """Project already-required atomic evidence into scene-level evidence.

    Asking QA to emit both matrices made it restate the same story coverage.
    The atomic rows remain model-judged; this grouping is deterministic.
    """
    if verdict.get('scene_coverage') is not None:
        return verdict  # Backward-compatible saved/test verdicts.
    expected = requirements(story)
    rows = verdict.get('requirement_coverage')
    if not expected or not isinstance(rows, list):
        return verdict
    by_id = {row.get('requirement_id'): row for row in rows if isinstance(row, dict)}
    scenes = []
    for scene in (story or {}).get('scenes', []):
        required = [item for item in expected if item['scene_number'] == scene['scene_number']]
        if not required:
            continue
        evidence = [by_id.get(item['id']) for item in required]
        complete = all(row and row.get('covered') is True for row in evidence)
        numbers = sorted({number for row in evidence if row for number in row.get('shot_numbers', [])
                          if isinstance(number, int) and not isinstance(number, bool)})
        scenes.append({'scene_number': scene['scene_number'], 'shot_numbers': numbers,
            'covered': complete,
            'evidence': (f"{len(required)} source requirement(s) covered by atomic review."
                         if complete else "One or more source requirements are uncovered; see linked issue evidence.")})
    return {**verdict, 'scene_coverage': scenes}


def check_review(verdict, shots, story):
    expected = {r['id']: r for r in requirements(story)}
    if not expected:
        return verdict
    rows = verdict.get('requirement_coverage')
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(r, dict) or not isinstance(r.get('requirement_id'), str) for r in rows)
            or {r['requirement_id'] for r in rows} != set(expected)):
        raise ValueError('Story review omitted required evidence. Your story is saved; retry planning.')
    by_number = {s['shot_number']: s for s in shots}
    checks = verdict.get('shot_checks')
    if (not isinstance(checks, list) or len(checks) != len(by_number)
            or any(not isinstance(c, dict) or type(c.get('shot_number')) is not int for c in checks)
            or {c['shot_number'] for c in checks} != set(by_number)):
        raise ValueError('Story review omitted shot execution checks. Retry planning.')
    for check in checks:
        if (not isinstance(check.get('consistent'), bool) or not isinstance(check.get('evidence'), str)
                or not check['evidence'].strip() or (not check['consistent'] and not any(
                    i.get('shot_number') == check['shot_number'] for i in verdict.get('issues', [])))):
            raise ValueError('Story review returned incomplete execution evidence. Retry planning.')
    for row in rows:
        requirement = expected[row['requirement_id']]
        numbers = row.get('shot_numbers')
        if (not isinstance(row.get('covered'), bool) or not isinstance(numbers, list)
                or any(type(n) is not int or n not in by_number or
                       by_number[n].get('scene_number') != requirement['scene_number'] for n in numbers)
                or len(numbers) != len(set(numbers)) or (row['covered'] and not numbers)
                or not isinstance(row.get('evidence'), str) or not row['evidence'].strip()):
            raise ValueError('Story review returned invalid requirement evidence. Retry planning.')
        if not row['covered']:
            # Require an actionable, linked defect, not a fabricated correction
            # assigned to the scene's first shot by deterministic code.
            if not any(i.get('requirement_id') == row['requirement_id'] and
                       i.get('shot_number') in by_number and
                       by_number[i['shot_number']].get('scene_number') == requirement['scene_number']
                       for i in verdict.get('issues', [])):
                raise ValueError('Story review found an omission without a repair target. Retry planning.')
    issues = list(verdict.get('issues', []))
    final_line = (story or {}).get('production_context', {}).get('explicit_final_line')
    if final_line:
        from app.agents.dialogue_integrity import contains_exact_line
        final_scene = (story or {}).get('scenes', [])[-1]['scene_number']
        matches = [shot for shot in shots if contains_exact_line(shot.get('dialogue_text'), final_line)]
        if len(matches) != 1 or matches[0].get('scene_number') != final_scene:
            candidates = [shot for shot in shots if shot.get('scene_number') == final_scene]
            if candidates and not any(issue.get('code') == 'missing_exact_final_line' for issue in issues):
                issues.append({'shot_number': candidates[-1]['shot_number'],
                    'code': 'missing_exact_final_line',
                    'problem': 'The explicit final spoken line from the brief is missing, changed, repeated or placed before the ending.',
                    'fix_instruction': 'Deliver the exact user-provided line once in the final scene; preserve all other dialogue and story beats.',
                    'requirement_id': '', 'repair_kind': 'structural', 'repair_fields': []})
    return {**verdict, 'issues': issues, 'approved': bool(verdict.get('approved')) and
            all(r['covered'] for r in rows) and all(c['consistent'] for c in checks) and not issues}
