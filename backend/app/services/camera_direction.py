"""Camera facts and deterministic, provider-neutral natural-language rendering.

No provider-specific magic tokens or promises of motion fidelity. Unknown legacy
instructions are rejected explicitly instead of silently becoming a static shot.
"""
import re

MOVEMENTS = {"hold", "dolly", "truck", "pan", "tilt", "track", "orbit", "crane", "pedestal", "zoom", "dolly_zoom", "roll"}
DIRECTIONS = {"none", "in", "out", "left", "right", "lateral", "up", "down", "clockwise", "counterclockwise", "follow"}
SPEEDS = {"none", "slow", "normal", "fast", "whip"}
STABILIZATIONS = {"locked", "smooth", "handheld"}
ALLOWED = {
    "hold": {"none"}, "dolly": {"in", "out", "left", "right", "lateral"},
    "truck": {"left", "right"}, "pan": {"left", "right", "none"},
    "tilt": {"up", "down", "none"}, "track": {"follow", "left", "right", "in", "out"},
    "orbit": {"clockwise", "counterclockwise", "none"},
    "crane": {"up", "down"}, "pedestal": {"up", "down"},
    "zoom": {"in", "out"}, "dolly_zoom": {"in", "out"},
    "roll": {"clockwise", "counterclockwise"},
}


def validate(spec):
    if not isinstance(spec, dict):
        raise ValueError("Camera direction must be a structured object")
    required = {"movement", "direction", "speed", "stabilization"}
    if set(spec) != required:
        raise ValueError("Camera direction requires movement, direction, speed and stabilization only")
    if any(not isinstance(value, str) for value in spec.values()):
        raise ValueError("Camera fields must be text enum values")
    if spec["movement"] not in MOVEMENTS or spec["direction"] not in DIRECTIONS or spec["speed"] not in SPEEDS or spec["stabilization"] not in STABILIZATIONS:
        raise ValueError("Unknown camera technique; normalize the instruction before compiling")
    if spec["direction"] not in ALLOWED[spec["movement"]]:
        raise ValueError("Camera direction is incompatible with its movement")
    if spec["movement"] == "hold" and spec["speed"] != "none":
        raise ValueError("A stationary camera cannot have travel speed")
    if spec["movement"] != "hold" and (spec["speed"] == "none" or spec["stabilization"] == "locked"):
        raise ValueError("A moving camera requires speed and cannot be locked off")
    if spec["speed"] == "whip" and spec["movement"] not in {"pan", "tilt"}:
        raise ValueError("Whip speed applies only to pan or tilt")
    return dict(spec)


def normalize_legacy(value):
    text = re.sub(r"[‐‑‒–—−-]", " ", (str(value or "").strip() or "static").casefold())
    text = re.sub(r"\b(dolly)(in|out)\b|\b(crane)(up|down)\b", lambda m: " ".join(x for x in m.groups() if x), text)
    if re.search(r"\b(?:then|followed by)\b", text):
        raise ValueError("Sequential camera moves require an explicit camera plan; no instruction was discarded")
    movement = None
    patterns = [("dolly_zoom", r"dolly\s+zoom"), ("dolly", r"\b(?:dolly|push|pull)\b"),
        ("truck", r"\b(?:truck|crab|slide)\b"), ("pan", r"\bpan(?:ning)?\b"),
        ("tilt", r"\btilt(?:ing)?\b"), ("track", r"\btrack(?:ing)?\b|\bfollow\b"),
        ("orbit", r"\borbit|\barc\b"), ("crane", r"\bcrane|\bjib\b|\bboom\b"),
        ("pedestal", r"\bpedestal\b"), ("zoom", r"\bzoom\b"), ("roll", r"\broll\b")]
    matches = [name for name, pattern in patterns if re.search(pattern, text)]
    if "dolly_zoom" in matches:
        matches = [m for m in matches if m not in {"dolly", "zoom"}]
    if len(matches) > 1:
        raise ValueError("Multiple camera moves require an explicit camera plan; no instruction was discarded")
    movement = matches[0] if matches else "hold"
    stationary = bool(re.search(r"\b(?:static|locked|fixed|stationary|motionless|unmoving|none)\b|no (?:camera )?(?:movement|motion)", text))
    handheld = bool(re.search(r"hand\s*held|\bsway\b", text))
    if not matches and not stationary and not handheld:
        raise ValueError("Unrecognized camera instruction; normalize it before compiling")
    if matches and stationary:
        raise ValueError("Conflicting stationary and moving camera instructions")
    direction = next((d for d in ("counterclockwise", "clockwise", "left", "right", "up", "down", "in", "out") if re.search(r"\b" + d + r"(?:ward|wards)?\b", text)), "none")
    if direction == "none" and movement == "dolly":
        direction = "lateral" if "lateral" in text else "in" if "forward" in text else "out" if "backward" in text else "none"
    if movement == "truck" and "slide" in text and direction in {"in", "out"}:
        movement = "dolly"  # A forward slider move is translation toward the subject.
    if movement == "pan" and direction in {"up", "down"}:
        movement = "tilt"  # Conventional name for a vertical pan; retain direction.
    if movement == "track" and direction == "none":
        direction = "follow"
    speed = "none" if movement == "hold" else "whip" if "whip" in text else "fast" if re.search(r"\b(?:fast|quick(?:ly)?|rapid(?:ly)?)\b", text) else "slow" if re.search(r"\b(?:slow(?:ly)?|gentl(?:e|y)|subtl(?:e|y)|slight(?:ly)?)\b", text) else "normal"
    return validate(dict(movement=movement, direction=direction, speed=speed,
                         stabilization="handheld" if handheld else "locked" if movement == "hold" else "smooth"))


def for_shot(shot):
    if shot.get("camera_direction") is None:
        return normalize_legacy(shot.get("camera_movement"))
    # Structured facts are authoritative. Never lexically validate the display
    # summary against them: that would reintroduce paraphrase false rejections.
    return validate(shot["camera_direction"])


def check_plan(qa, shots):
    """Feed invalid facts into the existing QA/FIX loop, never a second loop."""
    issues = []
    for shot in shots:
        try:
            for_shot(shot)
        except (ValueError, TypeError) as error:
            issues.append({"shot_number": shot["shot_number"], "problem": f"Invalid camera direction: {error}",
                           "fix_instruction": "Correct camera_direction and its camera_movement summary together; preserve the intended visual action and all dialogue. Use one explicit primary movement."})
    return {**qa, "approved": False, "issues": [*qa.get("issues", []), *issues]} if issues else qa


def render(spec):
    spec = validate(spec)
    move, direction = spec["movement"], spec["direction"]
    if move == "hold":
        return "Camera direction: hold position with subtle handheld sway." if spec["stabilization"] == "handheld" else "Camera direction: remain locked-off and motionless."
    actions = {
        "dolly": {"in": "dolly forward", "out": "dolly backward", "left": "travel left", "right": "travel right", "lateral": "dolly sideways"},
        "truck": {"left": "truck left", "right": "truck right"},
        "pan": {"none": "pan horizontally", "left": "pan left", "right": "pan right"},
        "tilt": {"none": "tilt vertically", "up": "tilt up", "down": "tilt down"},
        "track": {"follow": "track the moving subject", "left": "track left", "right": "track right", "in": "track forward", "out": "track backward"},
        "orbit": {"none": "arc around the subject", "clockwise": "orbit clockwise around the subject", "counterclockwise": "orbit counterclockwise around the subject"},
        "crane": {"up": "crane upward", "down": "crane downward"},
        "pedestal": {"up": "rise vertically without tilting", "down": "descend vertically without tilting"},
        "zoom": {"in": "zoom in without travelling", "out": "zoom out without travelling"},
        "dolly_zoom": {"in": "dolly forward while zooming out to preserve subject size", "out": "dolly backward while zooming in to preserve subject size"},
        "roll": {"clockwise": "roll clockwise around the optical axis", "counterclockwise": "roll counterclockwise around the optical axis"},
    }
    pace = {"slow": "slowly", "normal": "at an even pace", "fast": "quickly", "whip": "in a rapid whip movement"}[spec["speed"]]
    support = "with controlled handheld texture" if spec["stabilization"] == "handheld" else "with smooth motion"
    return f"Camera direction: {actions[move][direction]} {pace}, {support}."


def conflicting_prose(text, instruction):
    """Generated creative prose cannot own camera behavior; code owns this block.

Conservative syntactic guard, not a claim of complete natural-language inference.
Subject actions (steam drifts, hands tilt) are intentionally not camera commands.
"""
    text = text.replace(instruction, "")
    clauses = re.split(r"[.!?;]", text)
    behavior = r"\b(?:push\w*|pull\w*|pan(?:s|ning)?|tilt\w*|track\w*|dolly|dollies|orbit\w*|zoom\w*|crane\w*|roll\w*|static|locked|motionless|sway\w*|moves?|moving|widens?|tightens?)\b"
    qualifiers = r"(?:(?:does|do|not|never|must|should|will|can|remains?|stays?|is|are|keeps?|slowly|gently|quickly|smoothly|subtly|steadily|completely|perfectly|slightly)\s+){0,6}"
    camera_subject = r"\b(?:camera|viewpoint|framing|frame|view)\s+" + qualifiers + behavior
    imperative = r"^\s*(?:(?:slowly|gently|quickly|smoothly)\s+)?(?:push\s+in|pull\s+out|pan\s+(?:left|right)|tilt\s+(?:up|down)|dolly\b|orbit\b|zoom\s+(?:in|out))"
    return any(re.search(camera_subject, c, re.I) or re.search(imperative, c, re.I)
               or re.search(r"\bCamera direction:", c, re.I) for c in clauses)
