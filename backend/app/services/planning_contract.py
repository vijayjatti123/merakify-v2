"""Mechanical shot-plan checks; creative decisions stay in the existing Director/QA loop."""
import math
from app.services import camera_direction as camera


def creative_references(characters):
    # Keep all visual identity facts; URLs, database IDs and voice metadata do not
    # help choose framing. Originals remain authoritative in Director storage.
    return [{k: c[k] for k in ("name", "description", "gender", "age_bracket") if k in c} for c in characters]


def camera_options():
    return {"movement_directions": {k: sorted(v) for k, v in camera.ALLOWED.items()},
        "speeds": sorted(camera.SPEEDS), "stabilizations": sorted(camera.STABILIZATIONS),
        "rules": "hold needs speed none; moving cameras need non-none speed and non-locked stabilization; whip only pan/tilt"}


def render_camera_summaries(shots):
    for shot in shots:
        if shot.get("camera_direction") is not None:
            try:
                shot["camera_movement"] = camera.render(shot["camera_direction"]).removeprefix("Camera direction: ").rstrip(".")
            except (ValueError, TypeError):
                pass  # Existing camera QA reports it; never silently substitute hold.
    return shots


def check_mechanics(qa, shots, characters, minimum_shot_seconds=None):
    from app.services.dialogue_duration import MIN_SHOT_SECONDS
    minimum_shot_seconds = max(MIN_SHOT_SECONDS, minimum_shot_seconds or MIN_SHOT_SECONDS)
    names = {c["name"] for c in characters}
    issues = []
    for shot in shots:
        problems = []
        duration = shot.get("duration_sec")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            problems.append("duration_sec must be a finite positive number")
        elif minimum_shot_seconds is not None and duration < minimum_shot_seconds:
            problems.append(f"plan at least {minimum_shot_seconds:g} seconds of complete action per generated shot; combine compatible silent beats rather than buying tiny clips")
        elif not shot.get("has_dialogue") and duration > 15:
            problems.append("silent shots support at most 15 seconds")
        elif shot.get("speech_mode") == "voiceover" and duration > 15:
            problems.append("voiceover visuals retain the 15-second planning cap; never split or delete dialogue")
        duration_only = bool(problems)
        cast = shot.get("characters_in_shot", [])
        if not isinstance(cast, list) or any(not isinstance(n, str) or n not in names for n in cast):
            problems.append("characters_in_shot must contain only exact names from the reference library")
        if not isinstance(shot.get("has_dialogue"), bool):
            problems.append("has_dialogue must be boolean")
        if shot.get("has_dialogue") and not str(shot.get("dialogue_text") or "").strip():
            problems.append("spoken shots require the complete source utterance")
        if not shot.get("has_dialogue") and shot.get("dialogue_text"):
            problems.append("silent shots must not carry spoken text")
        if problems:
            issues.append({"shot_number": shot["shot_number"], "problem": "; ".join(problems),
                **({"code": "duration_bounds"} if duration_only and len(problems) == 1 else {}),
                "fix_instruction": "Correct only these mechanical violations, retaining story, camera choices and all complete source dialogue: " + "; ".join(problems)})
    return {**qa, "approved": False, "issues": [*qa.get("issues", []), *issues]} if issues else qa
