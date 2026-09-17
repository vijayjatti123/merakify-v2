"""Reusable voice registration inside the existing video task lifecycle.

Never clone on each render or substitute an unrelated voice sample. An uncertain
submission requires reconciliation, not an automatic second charge.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone

from app.services import job_service as jobs, audio_video_service as audio

CREATE_MODEL = "fal-ai/kling-video/create-voice"


def preparation(db, job_id, result, shot):
    names = shot.get("characters_in_shot", [])
    speaker = shot.get("speaker_label") or (names[0] if len(names) == 1 else "")
    matches = [c for c in result.get("continuity", {}).get("characters", [])
               if c.get("name", "").casefold() == speaker.casefold()]
    if len(matches) > 1:
        raise ValueError("The speaking character is ambiguous; resolve it before creating a Kling voice")
    character = matches[0] if matches else {}
    voice = shot.get("dialogue_voice_id")
    provider = shot.get("dialogue_audio_provider")
    if not voice or not provider:
        raise ValueError("Prepare approved speech with its recorded voice identity before using Kling")
    identity = [character.get("character_id") or f"{job_id}:{speaker}", provider, voice, result.get("language", "English")]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cached = jobs.read_kling_voice(db, key)
    if cached:
        if cached["status"] in {"failed", "submission_unknown"}:
            raise ValueError("The saved Kling voice needs provider reconciliation before retrying; no duplicate voice creation was sent")
        return key, None
    seconds = float(shot.get("dialogue_audio_duration_sec") or 0)
    if not 5 <= seconds <= 30:
        raise ValueError("First-time Kling voice setup needs 5–30 seconds of approved single-speaker audio. Use a longer approved line first, or select Seedance for this short line.")
    from app.services.video_generation_service import fresh_url
    return key, {"voice_url": fresh_url(shot["dialogue_audio_url"]), "identity": identity}


def ensure(db, key, sample):
    if sample is None:
        return jobs.read_kling_voice(db, key)
    data, claimed = jobs.claim_kling_voice(db, key, {
        "identity": sample["identity"], "sample_url": sample["voice_url"],
        "submitted_at": datetime.now(timezone.utc).isoformat()})
    if not claimed:
        return data
    try:
        raw = audio.fal_request("POST", "https://queue.fal.run/" + CREATE_MODEL,
                                {"voice_url": sample["voice_url"]})
        if not all(raw.get(k) for k in ("request_id", "status_url", "response_url")):
            raise ValueError("No complete Kling voice task receipt")
        return jobs.update_kling_voice(db, key, status="processing", **{k: raw[k] for k in ("request_id", "status_url", "response_url")})
    except Exception:
        jobs.update_kling_voice(db, key, status="submission_unknown")
        raise


def poll_voice(db, key):
    data = jobs.read_kling_voice(db, key)
    if not data:
        raise ValueError("Kling voice record is missing")
    if data["status"] == "submitting":
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["submitted_at"])).total_seconds()
        if age > 180:
            return jobs.update_kling_voice(db, key, status="submission_unknown")
        return data
    if data["status"] != "processing":
        return data
    try:
        state = audio.fal_request("GET", data["status_url"])
        if state.get("error") or state.get("status") in {"FAILED", "CANCELLED"}:
            return jobs.update_kling_voice(db, key, status="failed")
        if state.get("status") != "COMPLETED":
            return data
        result = audio.fal_request("GET", data["response_url"])
        voice_id = result.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            return jobs.update_kling_voice(db, key, status="failed")
        return jobs.update_kling_voice(db, key, status="ready", voice_id=voice_id, metrics=state.get("metrics"))
    except audio.FalResultError:
        return jobs.update_kling_voice(db, key, status="failed")


def bind(request, voice_id):
    request = copy.deepcopy(request)
    request["elements"][0]["voice_id"] = voice_id
    return request


def advance(db, job_id, shot):
    data = poll_voice(db, shot["video_kling_voice_key"])
    number = shot["shot_number"]
    if data["status"] in {"failed", "submission_unknown"}:
        jobs.update_video(db, job_id, number, expected_task_id=shot["video_task_id"],
                          video_status="review_required", video_error="Kling voice setup failed or needs reconciliation. No scene render was submitted. Choose another model or review the voice task.")
        jobs.append_event(db, job_id, "video_generation", f"Shot {number}: Kling voice setup requires attention; no scene render submitted.")
        return
    if data["status"] != "ready":
        return
    request = bind(shot["video_retry_request"], data["voice_id"])
    if not jobs.claim_kling_video_submission(db, job_id, number, shot["video_task_id"], request):
        return
    try:
        response = audio.submit(shot["video_model"], request)
        if not response.get("id"):
            raise ValueError("No Kling scene task receipt")
        jobs.update_video(db, job_id, number, video_status="processing", video_task_id=response["id"],
                          video_fal_status_url=response["status_url"], video_fal_response_url=response["response_url"],
                          video_kling_voice_id=data["voice_id"], video_error=None)
        jobs.append_event(db, job_id, "video_generation", f"Shot {number}: saved Kling voice bound to accepted scene; video submitted.")
    except Exception:
        jobs.update_video(db, job_id, number, video_status="submission_unknown",
                          video_error="Kling video submission needs reconciliation; it will not be resubmitted automatically.")
        raise
