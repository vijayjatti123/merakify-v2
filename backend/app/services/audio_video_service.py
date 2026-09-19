"""Audio-conditioned adapters within the existing durable video pipeline.

No post-generation lip-sync API, portrait substitution, or silent provider fallback.
"""
import math
import json
from typing import Literal
from urllib.parse import urlsplit

import httpx
import av

from app.config import settings
from app.services.still_frame_service import shot_fingerprint, visual_description

from app.video_models import VideoModel as AudioVideoModel
from app.services import video_references

DEFAULT = "seedance_evolink"
MODELS = {
    "h3_max_fal": ("fal", "minimax/h3-max/reference-to-video"),
    DEFAULT: ("evolink", "seedance-2.0-reference-to-video"),
    "seedance_fal": ("fal", "bytedance/seedance-2.0/reference-to-video"),
    "seedance_fast_evolink": ("evolink", "seedance-2.0-fast-reference-to-video"),
    "seedance_mini_evolink": ("evolink", "seedance-2.0-mini-reference-to-video"),
    "seedance_fast_fal": ("fal", "bytedance/seedance-2.0/fast/reference-to-video"),
    "seedance_mini_fal": ("fal", "bytedance/seedance-2.0/mini/reference-to-video"),
    "kling_voice_fal": ("fal", "fal-ai/kling-video/v3/4k/image-to-video"),
    "kling_avatar_fal": ("fal", "fal-ai/kling-video/ai-avatar/v2/pro"),
}


class FalResultError(ValueError):
    pass


def approved_speech_text(result, shot, speaker):
    """Keep the approved transcript beside its audio, outside visual-only prose."""
    text = shot.get("dialogue_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Approved dialogue text is missing. Restore it before generating this speaking video.")
    language = result.get("language") or "the language of the approved audio"
    # JSON quoting preserves native script, punctuation and embedded quotes.
    return ("\nApproved speech (transcript, not on-screen text): "
            + json.dumps({"speaker": speaker, "language": language, "dialogue": text}, ensure_ascii=False)
            + "\nSpeak these exact words in the specified language using the supplied audio's voice, pronunciation and timing. "
              "Do not translate, paraphrase, read these labels aloud, or display the transcript as captions.")


def validate_audio_result(media):
    """A successful HTTP response must contain decodable video AND speech audio.

    This is a technical gate, not proof of dialogue accuracy or perceptual sync.
    """
    try:
        media.seek(0)
        with av.open(media) as container:
            if not container.streams.video or next(container.decode(video=0), None) is None:
                raise ValueError("missing video")
        media.seek(0)
        with av.open(media) as container:
            if not container.streams.audio or next(container.decode(audio=0), None) is None:
                raise ValueError("missing audio")
    except Exception as error:
        raise ValueError("The speaking video has no usable audio/video track. Review this provider result before regenerating.") from error
    finally:
        media.seek(0)


def translate(result, shot, choice=None):
    from app.services.video_generation_service import fresh_url
    selected = result.get("video_model")
    if selected and choice and selected != choice:
        raise ValueError("Shots inherit the video model selected for this job")
    choice = selected or choice or shot.get("video_audio_model") or DEFAULT
    if choice not in MODELS:
        raise ValueError("Choose a supported audio-reference video model")
    if choice == "h3_max_fal":
        from app.services.h3_video_service import translate as h3_translate
        return h3_translate(result, shot)
    provider, model = MODELS[choice]
    if not shot.get("still_frame_url") or shot.get("still_frame_status") not in (None, "ready"):
        raise ValueError("Create or retry this shot's preview before generating its video")
    if shot.get("still_frame_source_hash") and shot["still_frame_source_hash"] != shot_fingerprint(shot):
        raise ValueError("This preview is out of date. Create a new preview before generating video")
    if not shot.get("compiled_prompt") or not shot.get("dialogue_audio_url"):
        raise ValueError("Finish the shot instructions and approved character audio first")
    duration = float(shot.get("dialogue_audio_duration_sec") or 0)
    # Keep this first integration bounded to single-shot performances.
    if not math.isfinite(duration) or not 0 < duration <= 15:
        raise ValueError("Audio-reference shots currently require measured speech of 15 seconds or less; split longer dialogue into complete lines")
    if not shot.get("speaker_label") and len(shot.get("characters_in_shot", [])) > 1:
        raise ValueError("Identify the speaking character before video generation")
    from app.services.dialogue_duration import performance_duration
    performance = performance_duration(shot.get("duration_sec") or 0, duration)
    if performance > 15:
        raise ValueError("Planned performance and speech must fit within 15 seconds; revise the shot without cutting dialogue")
    image, audio = fresh_url(shot["still_frame_url"]), fresh_url(shot["dialogue_audio_url"])
    direction = visual_description(shot["compiled_prompt"])
    # The preview establishes composition; separately labelled identities can
    # establish characters who enter later. The recording binds the speaker.
    import re
    direction = re.sub(r"https?://\S+", "the accepted scene reference", direction)
    speaker = shot.get("speaker_label") or ", ".join(shot.get("characters_in_shot", [])) or "the speaker"
    warnings = ["Audio-guided generation is experimental. Review the words, voice and mouth timing; the model may alter the recording."]
    manifest = []
    if choice == "kling_voice_fal":
        if result.get("language", "English").lower() not in {"english", "chinese", "en", "zh", "mandarin"}:
            raise ValueError("Kling voice-ID supports English/Chinese here. Choose Seedance for Indic dialogue; Kling would translate it to English.")
        if len(shot.get("characters_in_shot", [])) != 1:
            raise ValueError("Kling voice-ID currently requires one visible character to bind the voice unambiguously")
        if not shot.get("dialogue_text", "").strip():
            raise ValueError("Approved dialogue text is required for Kling voice-ID")
        prompt = direction + f'\n@Element1 is {speaker}. Preserve the approved scene. @Element1 says exactly: "{shot["dialogue_text"]}". No additional dialogue.'
        if len(prompt) > 2500:
            raise ValueError("Kling's scene and dialogue prompt exceeds 2,500 characters; shorten the shot instructions first")
        request = {"start_image_url": image, "prompt": prompt, "duration": str(math.ceil(performance)),
                   "generate_audio": True, "elements": [{"frontal_image_url": image, "reference_image_urls": [image]}]}
        warnings = ["Kling generates new speech using a reusable voice ID, not the exact Sarvam recording or timing. English/Chinese only; review the words and voice.",
                    "This Kling endpoint outputs 4K. The job's resolution setting does not change its output tier or price. First use also creates a voice ID from 5–30 seconds of approved speech."]
    elif choice == "kling_avatar_fal":
        request = {"image_url": image, "audio_url": audio,
                   "prompt": direction + f"\nAnimate {speaker} speaking the supplied audio. Preserve the accepted scene, clothing and identity."}
        warnings.append("Kling Avatar is experimental for scene motion. Resolution and duration are model-controlled, not selectable in this endpoint.")
    else:
        quality = result.get("quality", "720p").lower()
        supported = {"480p", "720p", "1080p"} if provider == "fal" else {"480p", "720p", "1080p", "4k"}
        if "_fast_" in choice or "_mini_" in choice:
            supported = {"480p", "720p"}
        if quality not in supported:
            raise ValueError("Selected audio-reference model does not support this resolution")
        image_tag, audio_tag = ("@Image1", "@Audio1") if provider == "fal" else ("@image1", "@audio1")
        references = video_references.build(result, shot, limit=9, tag_style=provider, refresh=fresh_url)
        manifest = references["manifest"]
        warnings.extend(references["warnings"])
        prompt = (f"{image_tag} is the approved scene and opening composition. {audio_tag} is {speaker}'s complete approved spoken performance.\n"
                  + references["instructions"] + "\n" + direction + f"\nUse {audio_tag} for the dialogue, voice, pronunciation, pauses and speaking timing. "
                  "Synchronize the visible speaker's mouth to it. Preserve the scene and perform the requested action. "
                  "Do not translate, paraphrase, add dialogue or substitute a different voice.")
        prompt += approved_speech_text(result, shot, speaker)
        prompt += f"\nComplete the approved action over {math.ceil(performance)} seconds. Keep speech at its natural reference pace; use the remaining time for the approved action and reaction, without extra words or repeating the line."
        seconds = math.ceil(performance)
        video_references.check_prompt(prompt, manifest)
        request = {"prompt": prompt, "image_urls": references["images"], "audio_urls": [audio],
                   "aspect_ratio": result.get("aspect_ratio", "16:9"), "generate_audio": True}
        if provider == "fal":
            request.update(resolution=quality, duration=str(seconds))
        else:
            request.update(model=model, quality=quality, duration=seconds)
        if seconds != duration:
            warnings.append(f"Video duration is {seconds}s for {duration:g}s of reference speech; generated audio is retained without forced trimming or replacement.")
    return {"provider": provider, "model": model, "audio_model": choice, "request": request,
            "mode": "voice_identity" if choice == "kling_voice_fal" else "audio_reference", "warnings": warnings, "mode_risk_terms": [],
            "reference_manifest": manifest,
            "constraints": "Accepted scene composition, labelled identities and approved speaker audio."}


def fal_request(method, url, body=None):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "queue.fal.run" or parsed.username or parsed.password:
        raise ValueError("Invalid fal queue URL")
    if not settings.fal_api_key.strip():
        raise ValueError("FAL_API_KEY is not configured")
    response = httpx.request(method, url, headers={"Authorization": "Key " + settings.fal_api_key},
                             json=body, timeout=60, follow_redirects=False)
    if response.is_error or response.is_redirect:
        if method == "GET" and response.status_code in (400, 422):
            raise FalResultError("fal rejected this generation. Review the saved task and inputs before regenerating")
        raise RuntimeError(f"fal HTTP {response.status_code}; check the saved task before retrying")
    return response.json()


def submit(model, request):
    if model not in {m for p, m in MODELS.values() if p == "fal"}:
        raise ValueError("Unsupported fal audio-reference endpoint")
    raw = fal_request("POST", "https://queue.fal.run/" + model, request)
    # Retain provider-returned URLs: subpath endpoint queue URLs need not match
    # the submitted endpoint path. Validate again before sending credentials.
    return {"id": raw.get("request_id"), "model": model,
            "status_url": raw.get("status_url"), "response_url": raw.get("response_url")}


def poll(shot):
    status = fal_request("GET", shot["video_fal_status_url"])
    if status.get("error") or status.get("status") in {"FAILED", "CANCELLED"}:
        return {"status": "failed", "error": "fal generation failed; inspect the saved provider task"}
    if status.get("status") != "COMPLETED":
        return {"status": "processing"}
    result = fal_request("GET", shot["video_fal_response_url"])
    return {"status": "completed", "model": shot["video_model"],
            "results": [result["video"]["url"]] if result.get("video", {}).get("url") else [],
            "usage": {"metrics": status.get("metrics"), "cost": result.get("cost"), "timings": result.get("timings")}}
