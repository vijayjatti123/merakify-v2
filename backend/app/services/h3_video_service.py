"""H3 Max request adapter; durable tasks and compliance remain in video_generation_service."""
import math
import re
from app.services import video_references
from app.services.still_frame_service import shot_fingerprint
from app.services.dialogue_duration import performance_duration
from app.services.speech_mode import is_voiceover, is_onscreen_speech


def translate(result, shot):
    from app.services.video_generation_service import fresh_url
    if not shot.get("compiled_prompt") or not shot.get("still_frame_url") or shot.get("still_frame_status") not in (None, "ready"):
        raise ValueError("Create this shot's accepted preview before generating video")
    if shot.get("still_frame_source_hash") and shot["still_frame_source_hash"] != shot_fingerprint(shot):
        raise ValueError("This preview is out of date; create a new preview first")
    speech = is_voiceover(shot) or is_onscreen_speech(shot)
    audio_seconds = float(shot.get("dialogue_audio_duration_sec") or 0) if speech else 0
    if speech and (not shot.get("dialogue_audio_url") or not math.isfinite(audio_seconds) or not 0 < audio_seconds <= 15):
        raise ValueError("Finish approved speech of at most 15 seconds before video generation")
    if is_onscreen_speech(shot) and len(shot.get("characters_in_shot", [])) > 1 and not shot.get("speaker_label"):
        raise ValueError("Identify the speaking character before video generation")
    planned = float(shot.get("duration_sec") or 0)
    if not math.isfinite(planned) or planned <= 0:
        raise ValueError("H3 Max requires a planned shot duration")
    seconds = performance_duration(planned, audio_seconds)
    if seconds > 15:
        raise ValueError("H3 Max requires shots of at most 15 seconds; preserve complete dialogue when splitting")
    duration = max(5, math.ceil(seconds))
    quality = result.get("quality", "720p")
    if quality not in {"480p", "720p"}:
        raise ValueError("H3 Max supports the platform's 480p and 720p settings (720p renders natively at 768p)")
    from app.services.dialogue_window import visual_instruction, prompt_instruction
    if speech:
        # The tested exact-audio endpoint pins the approved waveform to the
        # soundtrack and uses the accepted shot preview as its opening frame.
        # Its contract has no additional image-reference array.
        direction = visual_instruction(result, shot, "the supplied image")
        cast = shot.get("characters_in_shot") or []
        speaker = ("the off-screen narrator" if is_voiceover(shot) else
                   shot.get("speaker_label") or (cast[0] if len(cast) == 1 else None))
        prompt = (
            "One continuous shot.\n" + direction + "\n"
            + prompt_instruction(shot, duration, "the supplied soundtrack", exact_audio=True, speaker=speaker)
        )
        request = {"prompt": prompt, "image_url": fresh_url(shot["still_frame_url"]),
                   "target_audio_url": fresh_url(shot["dialogue_audio_url"]),
                   "duration": duration, "resolution": "480P" if quality == "480p" else "768P",
                   "prompt_expansion_mode": "disabled", "enable_safety_checker": True}
        return {"provider": "fal", "model": "minimax/h3-max/image-to-video",
                "request": request, "audio_model": "h3_max_fal",
                "reference_manifest": [], "warnings": [], "mode_risk_terms": [],
                "constraints": "", "mode": "exact_audio"}

    refs = video_references.build(result, shot, limit=12, tag_style="h3", refresh=fresh_url)
    direction = visual_instruction(result, shot, "Image 1", speaking=False)
    def replace_url(match):
        raw = match.group().rstrip('.,;)')
        tag = refs["lookup"].get(video_references.identity(raw))
        if not tag:
            raise ValueError("Compiled prompt contains an unbound reference")
        return tag + match.group()[len(raw):]
    direction = re.sub(r"https?://\S+", replace_url, direction)
    prompt = refs["instructions"] + "\n" + direction
    request = {"prompt": prompt, "reference_image_urls": refs["images"],
               "duration": duration, "resolution": "480P" if quality == "480p" else "768P",
               "aspect_ratio": result.get("aspect_ratio", "16:9"),
               "prompt_expansion_mode": "disabled", "enable_safety_checker": True}
    if request["aspect_ratio"] not in {"16:9", "9:16"}:
        raise ValueError("Choose landscape 16:9 or portrait 9:16")
    request["prompt"] += "\nAmbient sound only; no speech or music."
    video_references.check_prompt(request["prompt"], refs["manifest"])
    return {"provider": "fal", "model": "minimax/h3-max/reference-to-video", "request": request,
            **({"audio_model": "h3_max_fal"} if speech else {}),
            "reference_manifest": refs["manifest"], "warnings": refs["warnings"],
            "mode_risk_terms": [], "constraints": "", "mode": "reference_to_video"}
