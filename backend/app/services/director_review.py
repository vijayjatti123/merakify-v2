"""Code-only execution checks and timeline projection, not a second orchestrator."""
import math
from app.services.planning_contract import check_mechanics
from app.services.camera_direction import check_plan as check_camera
from app.services import ad_direction


def review(shots, characters, minimum=None, commercial=None):
    verdict = check_mechanics(check_camera({"approved": True, "issues": []}, shots), shots, characters, minimum)
    issues = list(verdict["issues"])
    def issue(number, text):
        issues.append({"shot_number": number, "problem": text, "code": "technical_plan"})
    if not shots:
        issue(0, "Add at least one shot.")
    audio_mode = (commercial or {}).get("ad_brief", {}).get("audio_mode", "auto")
    for shot in shots:
        if not shot.get("has_dialogue"):
            continue
        mode = shot.get("speech_mode", "onscreen")
        if audio_mode == "silent" or audio_mode in ("onscreen", "voiceover") and mode != audio_mode:
            issue(shot.get("shot_number", 0), f"Speech conflicts with your selected {audio_mode} treatment. Edit this shot's speech setting before approval.")
    numbers = [s.get("shot_number") for s in shots]
    if len(set(numbers)) != len(numbers):
        issue(0, "Shot numbers must be unique.")
    for s in shots:
        n = s.get("shot_number", 0)
        for field in ("description", "camera_angle", "lens", "lighting"):
            if not isinstance(s.get(field), str) or not s[field].strip():
                issue(n, f"Complete {field.replace('_', ' ')}.")
        for problem in ad_direction.problems(s):
            issue(n, problem)
        duration = s.get("duration_sec")
        if s.get("has_dialogue") and isinstance(duration, (int, float)) and duration > 15:
            issue(n, "Speaking-video shots support at most 15 seconds. Use separate complete lines; do not cut a sentence in half.")
        if s.get("has_dialogue") and s.get("speech_mode") != "voiceover":
            cast = s.get("characters_in_shot", [])
            if not s.get("speaker_name") and len(cast) == 1:
                s["speaker_name"] = cast[0]
            if s.get("speaker_name") not in cast:
                issue(n, "Choose the visible character who speaks this line.")
        if s.get("transition_after", "cut") not in ("cut", "crossfade", "match cut"):
            issue(n, "Choose a supported transition.")
    labels = {"duration_sec must be a finite positive number": "Enter a duration greater than zero seconds",
        "characters_in_shot": "Visible characters", "opening_characters": "Opening characters",
        "has_dialogue": "Speech setting", "dialogue_text": "Spoken line",
        "shot_direction": "Shot direction", "state_at_shot_start": "Opening image",
        "state_at_shot_end": "Ending image"}
    for finding in issues:
        for raw, label in labels.items():
            finding["problem"] = finding["problem"].replace(raw, label)
    return {"approved": not issues, "issues": issues, "review_mode": "user",
            "semantic_review_performed": False}


def timeline(shots, provisional=False):
    def duration(s):
        value = s.get("duration_sec")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0
    return {"total_duration_sec": sum(duration(s) for s in shots), "provisional": provisional,
            "transitions": [{"between": f"{a['shot_number']}-{b['shot_number']}",
                "type": a.get("transition_after", "cut"),
                "reason": "User-reviewed shot order"} for a, b in zip(shots, shots[1:])]}
