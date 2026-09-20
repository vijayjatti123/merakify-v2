"""Place approved speech inside the Director's ordered shot performance.

The Director chooses the speaking action beat.  Code turns that creative
decision and the measured recording length into deterministic seconds.  This
keeps provider prompting and final soundtrack muxing on one timing contract.
"""
import math
import re


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
    if not math.isfinite(duration) or not math.isfinite(audio) or duration <= 0 or audio <= 0:
        raise ValueError("Dialogue timing requires measurable shot and audio durations.")
    if audio > duration + .03:
        raise ValueError("The approved line is longer than the video shot.")
    # Centre speech on its authored beat. A small edge margin is retained when
    # the available room permits it; the line itself is never shortened.
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
    timing = shot.get("video_dialogue_timing") or shot.get("dialogue_timing") or {}
    start, end = timing.get("start_sec"), timing.get("end_sec")
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (start, end)):
        return timing
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


def prompt_instruction(shot, duration=None, reference_label="Audio 1"):
    """Describe one integrated audiovisual performance; never request a later mux."""
    seconds = float(duration if duration is not None else shot.get("duration_sec") or 0)
    timing = from_shot({**shot, "duration_sec": seconds}, require=True)
    beats = _beats(shot) or [str(shot.get("description") or "Perform the approved shot action.").strip()]
    rows = []
    for offset, beat in enumerate(beats):
        start = offset * seconds / len(beats)
        end = (offset + 1) * seconds / len(beats)
        rows.append(f"- {start:.2f}-{end:.2f}s: {beat}")
    voiceover = shot.get("speech_mode") == "voiceover"
    performance = (
        "The narration remains off-screen; no visible character moves their mouth as speech."
        if voiceover else
        "Before the line, the designated speaker's mouth remains closed or naturally at rest. "
        "Generate the voice and mouth performance together so articulation visibly matches every word."
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
