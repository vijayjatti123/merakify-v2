"""Module V: opt-in, durable per-shot restoration. Never part of Hedra generation."""
import base64
import hashlib
import json
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import av
import httpx
from PIL import Image

from app.config import settings
from app.db import SessionLocal
from app.models import FaceEnhancement
from app.services import job_service, storage_service

MODEL = "0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c"
MAX_FRAMES = 900
MAX_BYTES = 2 * 1024**3
log = logging.getLogger(__name__)


def download(url, path, limit=512*1024**2):
    if not url.startswith("https://"):
        raise ValueError("Media reference must use HTTPS.")
    size = 0
    with httpx.stream("GET", url, timeout=90, follow_redirects=True) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            for chunk in response.iter_bytes(1024*1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("Enhancement media exceeds the size limit.")
                output.write(chunk)


def restore_frame(db, task, client, frame, emit):
    payload = {"version": MODEL, "input": {"img": "data:image/png;base64,"+base64.b64encode(frame.read_bytes()).decode(), "scale": 2, "version": "v1.4"}}
    for retry in range(3):
        while True:
            wait = job_service.face_submission_slot(db)
            if not wait: break
            job_service.face_progress(db, task.id, expected_run_token=task._face_run_token)
            time.sleep(wait)
        submitted = time.time()
        emit({"event": "frame_submit", "frame": frame.stem, "submitted_at": submitted,
              "version": MODEL, "scale": 2, "gfpgan_version": "v1.4", "input_sha256": hashlib.sha256(frame.read_bytes()).hexdigest()})
        response = client.post("https://api.replicate.com/v1/predictions", json=payload,
                               headers={"Prefer": "wait=20", "Cancel-After": "120s"})
        if response.status_code != 429: break
        try: delay = float(response.json().get("retry_after", 15))
        except (ValueError, TypeError): delay = 15
        delay = min(max(delay, 10.5), 120)
        job_service.face_submission_slot(db, defer_seconds=delay)
        emit({"event": "rate_limit_backoff", "retry": retry+1, "seconds": delay})
    if response.status_code >= 400:
        raise ValueError(f"Replicate returned HTTP {response.status_code}; original video preserved.")
    prediction = response.json()
    prediction_id = prediction["id"]
    job_service.face_progress(db, task.id, expected_run_token=task._face_run_token, current_prediction_id=prediction_id)
    while prediction["status"] not in {"succeeded", "failed", "canceled"}:
        if time.time()-submitted > 150:
            raise ValueError("Replicate frame timed out; no uncertain paid resubmission was attempted.")
        time.sleep(2)
        job_service.face_progress(db, task.id, expected_run_token=task._face_run_token)
        response = client.get(f"https://api.replicate.com/v1/predictions/{prediction_id}")
        response.raise_for_status(); prediction = response.json()
    emit({"event": "frame_response", "frame": frame.stem, "id": prediction_id,
          "status": prediction["status"], "metrics": prediction.get("metrics"),
          "created_at": prediction.get("created_at"), "started_at": prediction.get("started_at"),
          "completed_at": prediction.get("completed_at")})
    if prediction["status"] != "succeeded":
        raise ValueError(f"Replicate frame {frame.stem} {prediction['status']}; original video preserved.")
    return prediction["output"]


def audio_digest(path):
    digest = hashlib.sha256()
    with av.open(str(path)) as source:
        if not source.streams.audio:
            raise ValueError("Source dialogue video has no audio track.")
        for packet in source.demux(audio=0):
            digest.update(bytes(packet))
    return digest.hexdigest()


def assemble(original, frames, target, fps, dimensions):
    silent = target.with_name("silent.mp4")
    with av.open(str(silent), "w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = dimensions
        stream.pix_fmt = "yuv420p"; stream.options = {"crf": "18", "preset": "fast"}
        for i, path in enumerate(frames):
            with Image.open(path) as image:
                frame = av.VideoFrame.from_image(image.convert("RGB")); frame.pts = i
            for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    binary = shutil.which("ffmpeg")
    if not binary:
        import imageio_ffmpeg
        binary = imageio_ffmpeg.get_ffmpeg_exe()
    command = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent), "-i", str(original),
               "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(target)]
    subprocess.run(command, capture_output=True, check=True, timeout=180)
    with av.open(str(target)) as result:
        stream = result.streams.video[0]
        count = sum(1 for _ in result.decode(video=0))
        if count != len(frames) or stream.average_rate != fps:
            raise ValueError("Enhanced video failed frame-count/frame-rate verification.")
    if audio_digest(original) != audio_digest(target):
        raise ValueError("Enhanced video failed original audio preservation check.")


def process(task_id):
    with SessionLocal() as db:
        task = db.get(FaceEnhancement, task_id)
        data = json.loads(task.data_json)
        task._face_run_token = data.get("run_token")
        def emit(fields):
            job_service.append_event(db, task.job_id, "face_enhancement", json.dumps({"shot_number": task.shot_number, **fields}))
        try:
            with tempfile.TemporaryDirectory(prefix="face-enhancement-") as folder:
                root = Path(folder); original = root/"original.mp4"
                download(storage_service.asset_url(data["source_key"]), original)
                expected = json.loads(data["source_json"]).get("video_sha256")
                if expected and hashlib.sha256(original.read_bytes()).hexdigest() != expected:
                    raise ValueError("Source video changed; enhancement canceled.")
                audio_digest(original)
                inputs = []
                with av.open(str(original)) as video:
                    stream = video.streams.video[0]; fps = stream.average_rate
                    if not fps or not 1 <= float(fps) <= 60:
                        raise ValueError("Unsupported source frame rate.")
                    size = 0
                    for i, frame in enumerate(video.decode(video=0)):
                        if i >= MAX_FRAMES or frame.width*frame.height > 4096*2160:
                            raise ValueError("Shot exceeds enhancement frame/dimension limits.")
                        if abs(float(frame.time or 0)-i/float(fps)) > 0.005:
                            raise ValueError("Variable frame timing is not supported; original video preserved.")
                        path = root/f"frame-{i:04d}.png"; frame.to_image().save(path)
                        size += path.stat().st_size
                        if size > MAX_BYTES: raise ValueError("Extracted frames exceed storage limit.")
                        inputs.append(path)
                if not inputs: raise ValueError("No video frames found.")
                job_service.face_progress(db, task.id, expected_run_token=task._face_run_token, total_frames=len(inputs))
                emit({"event": "extracted", "frames": len(inputs), "fps": str(fps)})
                outputs = []; dimensions = None
                with httpx.Client(headers={"Authorization": "Bearer "+settings.replicate_api_token}, timeout=60) as client:
                    for i, frame in enumerate(inputs):
                        # Stop spending if the user has regenerated or revised the source.
                        _, current = job_service.video_source(db, task.job_id, task.shot_number)
                        if current.get("video_key") != data["source_key"] or current.get("video_source_changed"):
                            raise ValueError("Shot changed during enhancement; current video preserved.")
                        url = restore_frame(db, task, client, frame, emit)
                        path = root/f"restored-{i:04d}.png"; download(url, path, 64*1024**2)
                        with Image.open(path) as image:
                            image.load()
                            if image.width*image.height > 8192*4320:
                                raise ValueError("Restored frame exceeds dimension limit.")
                            dimensions = dimensions or image.size
                            if image.size != dimensions:
                                raise ValueError("Restored frame dimensions changed within shot.")
                        size += path.stat().st_size
                        if size > MAX_BYTES: raise ValueError("Restoration exceeds temporary storage limit.")
                        outputs.append(path)
                        job_service.face_progress(db, task.id, expected_run_token=task._face_run_token, completed_frames=i+1)
                target = root/"enhanced.mp4"
                assemble(original, outputs, target, fps, dimensions)
                key = f"jobs/{task.job_id}/face-enhancement/{task.id}-{time.time_ns()}.mp4"
                with target.open("rb") as media:
                    stored = storage_service.upload_file(key, media, content_type="video/mp4")
                job_service.finish_face_enhancement(db, task.id, stored, hashlib.sha256(target.read_bytes()).hexdigest(), expected_run_token=task._face_run_token)
                emit({"event": "complete", "key": key, "frames": len(outputs), "audio": "original packets preserved"})
        except Exception as error:
            db.rollback()
            message = str(error) if isinstance(error, ValueError) else f"Face enhancement failed ({type(error).__name__}); original video preserved."
            try:
                job_service.face_progress(db, task.id, expected_run_token=task._face_run_token, status="failed", warning=message)
                emit({"event": "warning", "message": "WARNING: "+message})
            except Exception:
                log.exception("Could not persist enhancement failure; stale-worker recovery will mark it failed")


def polling_loop(stop):
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                task_id = job_service.next_face_enhancement(db)
            if task_id: process(task_id)
            else: stop.wait(2)
        except Exception:
            log.exception("Face enhancement queue error")
            stop.wait(5)
