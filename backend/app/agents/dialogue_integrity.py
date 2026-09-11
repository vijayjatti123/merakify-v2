"""Deterministic guards for the existing QA-triggered correction, not a pipeline stage."""
import re
import unicodedata
from collections import Counter
from copy import deepcopy


def _normalized_words(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = value.replace("'", "").replace("’", "")
    return " ".join(re.findall(r"\w+", value))


def protected_dialogue(shots, source_script_text):
    source = " " + _normalized_words(source_script_text) + " "
    return {
        shot["shot_number"]: deepcopy(shot)
        for shot in shots
        if (line := _normalized_words(shot.get("dialogue_text"))) and f" {line} " in source
    } if source_script_text else {}


def _dialogue_count_claim(problem):
    text = problem.casefold()
    spoken = re.search(r"dialogue|dialog|speech|spoken|speaking|voice.?over|has_dialogue", text)
    count = re.search(r"\bscene\b|\bshots\b|\bmultiple\b|\bextra\b|\bduplicate\b|\bseveral\b|\bcount\b|\bmore than\b", text)
    # Scene-local dialogue problems alone are not count claims (e.g. a wrong speaker).
    count_text = re.sub(r"\b(?:scene|shot)\s+\d+\b", "", text)
    multiplicity = re.search(r"\b(?:[2-9]|\d{2,}|two|three|four|five|six|seven|eight|nine|ten|multiple|extra|duplicate|several|both|twice|thrice|split|repeated|second|third|fourth|additional)\b|more than (?:one|1|a single)|too many", count_text)
    return bool(spoken and count and multiplicity)


def _claimed_count(problem):
    text = problem.casefold()
    numbers = {word: n for n, word in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
    token = r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
    # Do not mistake scene numbers or the threshold in 'more than one' for a count.
    text = re.sub(r"more than (?:one|1)", "multiple", text)
    text = re.sub(r"\bscene\s+\d+\b", "", text)
    match = re.search(r"\b" + token + r"\s+(?:(?:dialogue(?:-bearing)?|speaking|spoken|voice.?over)[ -]+)?shots?\b", text)
    if not match:
        match = re.search(r"(?:dialogue|speaking)\s+shots?\s*[:=]\s*" + token, text)
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else numbers[value]


def _changes_dialogue(instruction):
    text = instruction.casefold()
    if re.search(r"\bsilent\b|\bsilence\b|\bmute\b|has_dialogue.{0,15}false", text):
        return True
    if re.search(r"(?:remove|delete|drop|omit)\s+(?:the\s+)?shot\b", text):
        return True
    return bool(re.search(r"remove|delete|drop|clear|cut|shorten|rewrite|replace|alter|change|paraphrase|trim|omit|merge|combine", text)
                and re.search(r"dialogue|dialog|\bline\b|speech|words|spoken|has_dialogue", text))


def screen_issues(shots, issues, protected, notify):
    """Return unchanged valid issues, verified count-fix targets, and audit metadata."""
    by_number = {shot["shot_number"]: shot for shot in shots}
    counts = Counter(shot.get("scene_number") for shot in shots if shot.get("has_dialogue") is True)
    accepted, verified, rejected, limitations = [], set(), [], []
    for issue in issues:
        number = issue.get("shot_number")
        target = by_number.get(number)
        verified_count = False
        if _dialogue_count_claim(issue.get("problem", "")):
            scene = target.get("scene_number") if target else None
            actual = counts[scene]
            claimed = _claimed_count(issue["problem"])
            stated_scene = re.search(r"\bscene\s+(\d+)\b", issue["problem"], re.I)
            wrong_scene = stated_scene and str(scene) != stated_scene.group(1)
            if not target or actual <= 1 or wrong_scene or (claimed is not None and claimed != actual):
                note = f"Warning: QA claim rejected as factually incorrect for shot {number}, scene {scene}: actual has_dialogue:true count is {actual}"
                if claimed is not None:
                    note += f" (QA claimed {claimed})"
                note += "; issue discarded before FIX."
                notify("qa", note)
                rejected.append({"issue": issue, "actual_dialogue_count": actual, "scene_number": scene})
                continue
            verified_count = True
        if number in protected and _changes_dialogue(issue.get("fix_instruction", "")):
            note = f"Limitation: user-scripted line in shot {number} was protected from an automated fix; the requested dialogue change was rejected and this QA issue remains unresolved."
            notify("qa", note)
            limitations.append(issue)
            continue
        accepted.append(issue)
        if verified_count:
            verified.add(number)
    return accepted, verified, rejected, limitations


def restore_protected(shots, revised, protected, notify):
    """Enforce protection even if FIX ignores instructions or edits an unflagged shot."""
    result = list(revised)
    violations = []
    for number, original in protected.items():
        matches = [shot for shot in result if shot.get("shot_number") == number]
        if len(matches) == 1 and all(matches[0].get(key) == original.get(key) for key in ("dialogue_text", "has_dialogue", "scene_number")):
            continue
        issue = {"shot_number": number, "problem": "FIX changed protected user-scripted dialogue", "fix_instruction": "Preserve the original scripted line; automatic correction remains unresolved"}
        violations.append(issue)
        notify("qa", f"Limitation: user-scripted line in shot {number} was protected from an automated fix; rejected FIX's returned change and restored the original shot.")
        position = next((i for i, s in enumerate(result) if s.get("shot_number") == number), None)
        if position is None:
            # Reinsert before the next surviving original neighbor, preserving shot order.
            old_index = next(i for i, s in enumerate(shots) if s["shot_number"] == number)
            following = {s["shot_number"] for s in shots[old_index + 1:]}
            position = next((i for i, s in enumerate(result) if s.get("shot_number") in following), len(result))
        result = [s for s in result if s.get("shot_number") != number]
        result.insert(position, deepcopy(original))
    return result, violations


def warn_dialogue_loss(before, after, verified, notify):
    """Visibility-only safety net; a verified reason exempts only its own target."""
    warnings = []
    for scene in dict.fromkeys(s.get("scene_number") for s in before):
        old = [s for s in before if s.get("scene_number") == scene]
        new = [s for s in after if s.get("scene_number") == scene]
        old_count = sum(s.get("has_dialogue") is True for s in old)
        new_count = sum(s.get("has_dialogue") is True for s in new)
        old_text = Counter(s.get("dialogue_text", "") for s in old if s.get("dialogue_text", "").strip())
        new_text = Counter(s.get("dialogue_text", "") for s in new if s.get("dialogue_text", "").strip())
        missing = old_text - new_text
        allowance = max(0, old_count - 1)
        exempt_count = 0
        for shot in old:
            if shot["shot_number"] not in verified or not allowance or shot.get("has_dialogue") is not True:
                continue
            candidates = [s for s in new if s.get("shot_number") == shot["shot_number"]]
            if not candidates or (len(candidates) == 1 and candidates[0].get("has_dialogue") is False and not candidates[0].get("dialogue_text", "").strip()):
                text = shot.get("dialogue_text", "")
                if missing[text]: missing[text] -= 1
                allowance -= 1
                exempt_count += 1
        if sum(missing.values()) or old_count - new_count > exempt_count:
            note = f"Warning: dialogue-loss event after QA/FIX in scene {scene}: has_dialogue count {old_count} -> {new_count}; {sum(missing.values())} non-empty dialogue text occurrence(s) lost or altered without a verified-correct count reason."
            notify("qa", note)
            warnings.append(note)
    return warnings
