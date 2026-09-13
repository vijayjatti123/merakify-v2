"""Hedra dialogue adapter for Module R's existing durable video task flow."""
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import av
import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.services import job_service, storage_service
from app.services.still_frame_service import fresh_reference
from app.services.voice_generation_service import decoded_audio_duration

MODEL = "hedra-character-3"
BASE = "https://api.hedra.com/v3"
TOLERANCE = 0.05
PROMPT = ("The character speaks naturally with the supplied audio, with subtle head movement "
          "and facial expressions. Preserve the character identity, clothing and rendering style.")


class MediaValidationError(ValueError):
    """Completed media needs review; repeating a paid render is not automatic."""


def ffmpeg():
    executable = shutil.which("ffmpeg")
    if not executable:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    return executable


def preview(result, shot):
    if not shot.get("has_dialogue"):
        raise ValueError("Hedra is reserved for dialogue shots")
    if not shot.get("compiled_prompt") or not shot.get("dialogue_audio_url"):
        raise ValueError("Finish compilation and approved dialogue audio before Hedra generation")
    names = {n.strip().casefold() for n in shot.get("characters_in_shot", [])}
    characters = [c for c in result.get("continuity", {}).get("characters", [])
                  if c.get("name", "").strip().casefold() in names]
    speaker = shot.get("speaker_label", "").strip().casefold()
    selected = [c for c in characters if c.get("name", "").strip().casefold() == speaker]
    if not selected and len(characters) == 1:
        selected = characters
    if len(characters) > 1 and not selected:
        raise ValueError("Dialogue speaker is ambiguous; identify the speaker before paid generation")
    character = selected[0] if selected else {}
    vault = bool(character.get("character_id"))
    # Continuity's image_url has already been resolved by Module H. Never reload
    # the base vault image here: that would bypass the job's locked style variant.
    image = character.get("image_url") if vault else shot.get("still_frame_url")
    if not image:
        raise ValueError("Dialogue video needs its locked vault image or an accepted Module O still")
    ratio = result.get("aspect_ratio", "16:9")
    if ratio not in {"1:1", "4:3", "3:4", "16:9", "9:16", "9:21", "21:9"}:
        raise ValueError("Unsupported Hedra aspect ratio")
    return {"provider": "hedra", "request": {"input": {
                "start_image": {"source": "url", "url": fresh_reference(image)},
                "audio": {"source": "url", "url": fresh_reference(shot["dialogue_audio_url"])},
                "prompt": PROMPT, "aspect_ratio": ratio, "resolution": "720p"}},
            "reference_source": "vault_style_resolved" if vault else "module_o_still",
            "character_id": character.get("character_id"),
            "warnings": ["Hedra dialogue video uses approved audio; final duration is verified after local trimming.",
                         "Input is fitted to the requested aspect ratio with matte borders when needed; no face is cropped."],
            "mode_risk_terms": [], "constraints": "Preserve the supplied identity and audio performance."}


def api(method, path, *, body=None, files=None):
    if not settings.hedra_api_key.strip():
        raise ValueError("HEDRA_API_KEY is not configured")
    response = httpx.request(method, BASE + path,
                            headers={"Authorization": "Key " + settings.hedra_api_key},
                            json=body, files=files, timeout=180 if method == "POST" else 60)
    if response.is_error:
        # Provider messages may contain private URLs/input text. Keep shared traces safe.
        raise RuntimeError(f"Hedra HTTP {response.status_code}; inspect provider account before retrying")
    return response.json()


def download(url, limit):
    if urlsplit(url).scheme != "https":
        raise ValueError("Hedra media references require HTTPS")
    data = bytearray()
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(1024 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Media exceeds download size limit")
    return bytes(data)


def fit_image(data, ratio):
    """Preserve every identity pixel; avoid destructive automatic face crops."""
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        w, h = map(int, ratio.split(":"))
        size = (round(720 * w / h), 720) if w >= h else (720, round(720 * h / w))
        fitted = ImageOps.pad(image, size, method=Image.Resampling.LANCZOS, color=(32, 32, 32))
        output = io.BytesIO()
        fitted.save(output, format="JPEG", quality=95)
        return output.getvalue(), {"original_size": list(image.size), "input_size": list(size),
                                   "matte_added": abs(image.width / image.height - w / h) > 0.001}


def start(db, job_id, number, result, shot):
    from app.services.video_generation_service import source_fingerprint
    translated = preview(result, shot)
    if not settings.hedra_api_key.strip():
        raise ValueError("HEDRA_API_KEY is not configured")
    ffmpeg()  # Fail before spending if the local post-processing runtime is unavailable.
    inputs = translated["request"]["input"]
    audio = download(inputs["audio"]["url"], 100 * 1024 * 1024)
    duration = decoded_audio_duration(audio)
    if not math.isfinite(duration) or not 0.5 <= duration <= 600:
        raise ValueError("Hedra audio must contain 0.5 to 600 seconds of decoded speech")
    image, framing = fit_image(download(inputs["start_image"]["url"], 10 * 1024 * 1024), inputs["aspect_ratio"])
    # Snapshot the exact audio consumed by this paid task. Replanning must not change
    # the trim target or audio bytes of a task already in flight.
    job_service.claim_video(db, job_id, number, {
        "video_status": "submitting", "video_provider": "hedra", "video_model": MODEL,
        "video_error": None, "video_source_hash": source_fingerprint(shot),
        "video_submitted_at": datetime.now(timezone.utc).isoformat(),
        "video_warnings": translated["warnings"], "video_target_duration_sec": duration,
        "video_reference_audio_sha256": hashlib.sha256(audio).hexdigest(),
        "video_reference_source": translated["reference_source"],
        "video_reference_character_id": translated["character_id"], "video_input_framing": framing,
        "video_aspect_ratio": inputs["aspect_ratio"], "video_resolution": "720p"})
    try:
        refs = {}
        for name, data, mime in [("start_image", image, "image/jpeg"), ("audio", audio, "audio/wav")]:
            upload = api("POST", "/files", files={"file": (name, data, mime)})
            refs[name] = {"source": "url", "url": upload["url"]}
        request = {"input": {**inputs, **refs}}  # No duration_ms: trim against decoded audio locally.
        response = api("POST", "/models/" + MODEL, body=request)
        task = response.get("job_id")
        if not isinstance(task, str) or not task:
            raise ValueError("Hedra returned no job ID; submission outcome unknown")
    except Exception as error:
        db.rollback()
        message = f"Hedra submission uncertain/failed ({type(error).__name__}); reconcile provider account before retrying."
        job_service.update_video(db, job_id, number, video_status="submission_unknown", video_error=message)
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: {message}")
        raise
    job_service.update_video(db, job_id, number, video_task_id=task, video_status="processing",
                             video_requested_duration=duration, video_generate_audio=True)
    job_service.append_event(db, job_id, "video_generation",
        f"Shot {number}: Hedra task {task} submitted using {translated['reference_source']}; "
        f"decoded audio {duration:.6f}s, SHA256 {hashlib.sha256(audio).hexdigest()}; "
        f"input framing {framing}; no duration_ms, verified post-trim required.")
    return response


def measure(path):
    """Decode complete tracks; container metadata alone can hide a trailing frame."""
    with av.open(str(path)) as container:
        videos = container.streams.video
        if not videos:
            raise MediaValidationError("Hedra output has no video track")
        stream = videos[0]
        rate = float(stream.average_rate or 25)
        frames = 0
        end = 0.0
        for frame in container.decode(video=0):
            frames += 1
            end = max(end, float(frame.time or 0) + (float(frame.duration * frame.time_base) if frame.duration else 1 / rate))
        size = [stream.width, stream.height]
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise MediaValidationError("Hedra output has no audio track")
        audio_end = 0.0
        for frame in container.decode(audio=0):
            audio_end = max(audio_end, float(frame.time or 0) + frame.samples / frame.sample_rate)
    if not frames or not audio_end:
        raise MediaValidationError("Hedra output contains an empty track")
    return {"video_duration_sec": end, "audio_duration_sec": audio_end, "frames": frames, "dimensions": size}


def trim(source, destination, duration):
    original = measure(source)
    if min(original["video_duration_sec"], original["audio_duration_sec"]) < duration - TOLERANCE:
        raise MediaValidationError("Hedra output is shorter than approved audio; refusing to hide missing performance")
    attempts = []
    for mode, codecs in [("stream_copy", ["-c:v", "copy", "-c:a", "copy"]),
                         ("reencode", ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac"])]:
        command = [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a:0", "-t", f"{duration:.9f}", *codecs, "-movflags", "+faststart", str(destination)]
        completed = subprocess.run(command, capture_output=True, timeout=300)
        if completed.returncode:
            attempts.append({"mode": mode, "passed": False, "reason": "FFmpeg failed"})
            continue
        measured = measure(destination)
        passed = all(abs(measured[k] - duration) <= TOLERANCE for k in ("video_duration_sec", "audio_duration_sec"))
        attempts.append({"mode": mode, "passed": passed, **measured})
        if passed:
            return {"source": original, "target_duration_sec": duration, "trim_attempts": attempts, "final": measured}
    raise MediaValidationError("Local trim could not meet the 50ms audio/video tolerance; review required")


def poll(db, job_id, shot):
    number = shot["shot_number"]
    response = api("GET", "/jobs/" + quote(shot["video_task_id"], safe=""))
    if response.get("model") != MODEL:
        job_service.update_video(db, job_id, number, video_status="review_required", video_error="Hedra task model mismatch")
        job_service.append_event(db, job_id, "video_generation", f"WARNING: Shot {number}: Hedra task model mismatch; stopped for review.")
        return
    if response.get("status") == "FAILED":
        job_service.update_video(db, job_id, number, video_status="failed", video_error="Hedra generation failed; inspect provider task before retrying")
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: Hedra task failed; no paid regeneration attempted.")
        return
    if response.get("status") != "COMPLETED":
        return
    outputs = response.get("outputs") or []
    if len(outputs) != 1 or not outputs[0].get("url"):
        job_service.update_video(db, job_id, number, video_status="review_required", video_error="Completed Hedra task did not return one usable video")
        job_service.append_event(db, job_id, "video_generation", f"WARNING: Shot {number}: completed Hedra task has no usable output; stopped for review.")
        return
    try:
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "hedra.mp4", Path(directory) / "trimmed.mp4"
            source.write_bytes(download(outputs[0]["url"], 512 * 1024 * 1024))
            evidence = trim(source, target, float(shot["video_target_duration_sec"]))
            w, h = evidence["final"]["dimensions"]
            rw, rh = map(int, shot["video_aspect_ratio"].split(":"))
            if abs((w / h) / (rw / rh) - 1) > 0.02:
                raise MediaValidationError("Hedra output aspect ratio does not match the job; review required")
            key = f"jobs/{job_id}/videos/{number}-{shot['video_task_id']}.mp4"
            raw = target.read_bytes()
            with target.open("rb") as media:
                stored = storage_service.upload_file(key, media, content_type="video/mp4")
    except MediaValidationError as error:
        job_service.update_video(db, job_id, number, video_status="review_required", video_error=str(error))
        job_service.append_event(db, job_id, "video_generation", f"WARNING: Shot {number}: {error}; no paid regeneration attempted.")
        return
    job_service.update_video(db, job_id, number, video_status="done", video_error=None,
        video_url=stored["url"], video_key=key, video_sha256=hashlib.sha256(raw).hexdigest(), video_bytes=len(raw),
        video_usage={"cost": response.get("cost"), "currency": response.get("currency")},
        video_trim=evidence, video_stored_at=datetime.now(timezone.utc).isoformat())
    job_service.append_event(db, job_id, "video_generation",
        f"Shot {number}: Hedra video trimmed and persisted to S3 key {key}; "
        f"target {shot['video_target_duration_sec']:.6f}s; verification {json.dumps(evidence)}. "
        "Duration checks do not certify perceptual lip-sync or whether a final gesture feels complete.")
