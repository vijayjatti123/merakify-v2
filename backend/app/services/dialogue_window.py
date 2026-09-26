"""Place approved speech inside the Director's ordered shot performance.

The Director chooses the speaking action beat.  Code turns that creative
decision and the measured recording length into deterministic seconds.  This
keeps provider prompting and final soundtrack muxing on one timing contract.
"""
import math
import re
import json


_SPEECH_CUES = re.compile(
    r"\b(says?|speaks?|asks?|answers?|replies?|whispers?|shouts?|calls?|utters?|"
    r"delivers?|announces?|narrates?|voice[- ]?over|dialogue|line)\b",
    re.IGNORECASE,
)


def _beats(shot):
    beats = (shot.get("shot_direction") or {}).get("action_beats") or []
    return [beat.strip() for beat in beats if isinstance(beat, str) and beat.strip()]


def beat_index(shot):
    """Return a 1-based speaking beat, or None when legacy intent is unclear."""
    beats = _beats(shot)
    # Plans saved before ordered action beats existed still need a safe upgrade
    # path. Centre the line in the whole performance instead of assuming 0:00.
    if not beats:
        return 1, "legacy_whole_shot"
    explicit = (shot.get("shot_direction") or {}).get("dialogue_beat_index")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and 1 <= explicit <= len(beats):
        return explicit, "director"
    matches = [index for index, beat in enumerate(beats, 1) if _SPEECH_CUES.search(beat)]
    if len(matches) == 1:
        return matches[0], "legacy_action_beat"
    return None, "unavailable"


def calculate(shot, audio_duration, shot_duration=None):
    """Return an exact speech window without inventing an ambiguous placement."""
    beats = _beats(shot) or ["whole shot"]
    index, source = beat_index(shot)
    duration = float(shot_duration if shot_duration is not None else shot.get("duration_sec") or 0)
    audio = float(audio_duration or 0)
    if not index:
        raise ValueError("Dialogue timing is missing. Refresh this shot's direction before generating video.")
    speaking_beat = beats[index - 1]
    if _SPEECH_CUES.search(speaking_beat) and re.search(r"\b(?:before|after)\b", speaking_beat, re.I):
        raise ValueError("The speaking beat also depends on another action. Put that action in its own earlier or later beat before generating video.")
    if not math.isfinite(duration) or not math.isfinite(audio) or duration <= 0 or audio <= 0:
        raise ValueError("Dialogue timing requires measurable shot and audio durations.")
    if audio > duration + .03:
        raise ValueError("The approved line is longer than the video shot.")
    # The Director has already separated prerequisite action into an earlier
    # beat, so centre the complete line on its dedicated delivery beat.
    centre = ((index - .5) / len(beats)) * duration
    margin = min(.15, max(0.0, (duration - audio) / 2))
    start = min(max(centre - audio / 2, margin), duration - audio - margin)
    start = max(0.0, start)
    end = start + audio
    return {
        "beat_index": index,
        "beat_count": len(beats),
        "start_sec": round(start, 3),
        "end_sec": round(end, 3),
        "audio_duration_sec": round(audio, 3),
        "source": source,
    }


def from_shot(shot, *, require=False):
    # Current direction is authoritative for a new submission. Persisted
    # dialogue_timing/video_dialogue_timing can describe an older plan or clip;
    # never let either silently move a newly generated line to its old window.
    try:
        audio = float(shot.get("dialogue_audio_duration_sec") or 0)
        duration = float(shot.get("duration_sec") or 0)
        if audio > duration:
            from app.services.dialogue_duration import performance_duration
            duration = performance_duration(duration, audio)
        return calculate(shot, audio, duration)
    except (TypeError, ValueError):
        if require:
            raise
        return None


def prompt_instruction(shot, duration=None, reference_label="Audio 1", *, exact_audio=False, speaker=None):
    """Describe one integrated audiovisual performance; never request a later mux."""
    seconds = float(duration if duration is not None else shot.get("duration_sec") or 0)
    timing = from_shot({**shot, "duration_sec": seconds}, require=True)
    beats = _beats(shot) or [str(shot.get("description") or "Perform the approved shot action.").strip()]
    # Make the displayed beat windows agree with the exact speech window.
    # The speaking beat may expand to contain the whole approved line; only
    # the surrounding silent/action beats are compressed.
    boundaries = [offset * seconds / len(beats) for offset in range(len(beats) + 1)]
    speaking = timing["beat_index"] - 1
    boundaries[speaking] = min(boundaries[speaking], timing["start_sec"])
    boundaries[speaking + 1] = max(boundaries[speaking + 1], timing["end_sec"])
    rows = []
    for offset, beat in enumerate(beats):
        start = boundaries[offset]
        end = boundaries[offset + 1]
        rows.append(f"- {start:.2f}-{end:.2f}s: {beat}")
    voiceover = shot.get("speech_mode") == "voiceover"
    if exact_audio:
        line = shot.get("dialogue_text")
        if not isinstance(line, str) or not line.strip():
            raise ValueError("Approved dialogue text is missing. Restore it before generating this speaking video.")
        if not (0 <= timing["start_sec"] < timing["end_sec"] <= seconds + .03):
            raise ValueError("Approved speech timing falls outside this shot; revise its action beats before generating video.")
        speaker = speaker or ("the off-screen narrator" if voiceover else shot.get("speaker_label"))
        if not speaker:
            raise ValueError("Identify the speaking character before video generation.")
        mouth = (("No visible person moves their mouth to speak. " if shot.get("characters_in_shot") else "") if voiceover else
                 f"Only {speaker} visibly articulates this line; every other character remains silent. ")
        before = (f"From 0 to {timing['start_sec']:.3f}s, the soundtrack contains no speech. " if voiceover and not shot.get("characters_in_shot") else
                  f"From 0 to {timing['start_sec']:.3f}s, everyone is silent and mouths stay closed or naturally at rest. ")
        after = (f"From {timing['end_sec']:.3f}s to the end, the soundtrack contains no speech. " if voiceover and not shot.get("characters_in_shot") else
                 f"From {timing['end_sec']:.3f}s to the end, everyone is silent and mouths return to rest. ")
        return (
            "PERFORMANCE TIMELINE — one continuous shot, in this exact order:\n" + "\n".join(rows)
            + "\n" + before
            + f"From {timing['start_sec']:.3f}s to {timing['end_sec']:.3f}s, {speaker} says exactly once: "
            + json.dumps(line, ensure_ascii=False) + "\n" + mouth
            + after
            + f"{reference_label.capitalize()} is the only audio authority: use its exact waveform and timing. "
            + "Do not generate, move, repeat, translate or paraphrase speech. No extra words, muttering, "
              "filler, narration or captions. Perform only the approved action beats; never change screen sides "
              "or add a cut to fit them."
        )
    performance = (
        "The narration remains off-screen; no visible character moves their mouth as speech."
        if voiceover else
        "Before the line, the designated speaker's mouth remains closed or naturally at rest. "
        "Generate the voice and mouth performance together so articulation visibly matches every word. "
        "After the line, return the mouth to rest. Other visible characters never speak."
    )
    return (
        "PERFORMANCE TIMELINE — one continuous shot, in this exact order:\n" + "\n".join(rows)
        + f"\nThe approved line begins only at {timing['start_sec']:.3f}s and finishes by "
          f"{timing['end_sec']:.3f}s, during action beat {timing['beat_index']}. "
        + performance + " "
        + f"{reference_label} is a voice, accent and pronunciation reference only. Its file onset is not "
          "the dialogue start time and its waveform must not be laid over the beginning of the video. "
          "Say the approved line exactly once. No earlier words, muttering, breaths used as speech, filler, "
          "paraphrase, repeated line or additional dialogue."
    )


def visual_instruction(result, shot, opening_label, *, speaking=True):
    """Compact shot direction from approved physical facts and ordered beats."""
    direction = shot.get("shot_direction") or {}
    from app.services.ad_direction import shot_visual_style
    style = shot_visual_style(result, shot)
    if isinstance(style, dict):
        style = "; ".join(str(style.get(key) or "").strip() for key in
                          ("rendering", "palette", "lighting_motif", "texture_grain") if style.get(key))
    camera = [str(shot.get(key) or "").strip() for key in ("camera_angle", "camera_movement")]
    if shot.get("lens"):
        camera.append(f"lens {shot['lens']}")
    camera = "; ".join(value for value in camera if value)
    def sentence(label, value):
        value = str(value or "").strip().rstrip(".")
        return f"{label}: {value}." if value else ""
    parts = [
        f"Use {opening_label} as the exact opening composition and physical state.",
        sentence("Locked visual style", style),
        sentence("Camera", camera),
        sentence("Lighting", shot.get("lighting")),
        sentence("Opening state", shot.get("state_at_shot_start")),
        sentence("Blocking", direction.get("blocking")),
        sentence("Physical support/contact", direction.get("support_and_contact")),
        sentence("Product/props", direction.get("product_props")),
    ]
    paths = direction.get("entry_exit_paths") or []
    if paths:
        parts.append(sentence("Entry/exit path", " ".join(paths)))
    if not speaking:
        beats = _beats(shot)
        # Ordered Director beats own the action. The card synopsis may use a
        # different tense or mention the ending, so never send both as orders.
        action = "" if beats else shot.get("description") or ""
        if not action and not beats and shot.get("compiled_prompt"):
            from app.services.still_frame_service import visual_description
            action = visual_description(shot["compiled_prompt"])
        if action:
            parts.append(sentence("Action", action))
        if beats:
            parts.append("Ordered visible beats: " + " ".join(f"{i}) {beat}" for i, beat in enumerate(beats, 1)))
        if direction.get("performance"):
            parts.append(sentence("Performance", direction['performance']))
    parts.extend([
        sentence("Required outcome", direction.get("critical_outcome")),
        sentence("Ending state", shot.get("state_at_shot_end")),
    ])
    invariants = direction.get("spatial_invariants") or []
    forbidden = direction.get("forbidden_geometry") or []
    if not shot.get("characters_in_shot"):
        forbidden = [rule for rule in forbidden if not re.search(r"\b(?:human|people|person|characters?|hands?)\b", rule, re.I)]
        parts.append("Every frame keeps the approved opening's subjects and setting.")
    if invariants:
        parts.append("Keep throughout: " + " ".join(invariants))
    if forbidden:
        parts.append("Never show: " + " ".join(forbidden))
    parts.append("The PERFORMANCE TIMELINE below is the only authority for action order and speech; do not infer or add dialogue from any other visual instruction."
                 if speaking else "Follow the ordered visible beats in one continuous shot; no dialogue, muttering, extra action or scene cuts.")
    return "\n".join(part for part in parts if part)
