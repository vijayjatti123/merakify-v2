"""Spoken audio is independent of whether a face is performing it."""
def is_voiceover(shot):
    if not shot.get("has_dialogue"):
        return False
    mode = shot.get("speech_mode")
    if mode not in (None, "voiceover", "onscreen", "none"):
        raise ValueError("Unknown shot speech_mode")
    return mode == "voiceover" or (mode is None and not shot.get("characters_in_shot"))


def is_onscreen_speech(shot):
    return bool(shot.get("has_dialogue")) and not is_voiceover(shot)


# Compatibility for older callers; this describes speech, not provider routing.
uses_hedra = is_onscreen_speech
