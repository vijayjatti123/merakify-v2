"""Replace generated dialogue audio with the exact approved recording.

The video model still receives that recording to drive the visible performance.
This local mux is not lip sync and makes no provider call: it removes the model's
new soundtrack, keeps its video frames, and pads the approved line with silence.
"""
import hashlib
import io
import math
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

import av
import httpx

from app.services import storage_service


def _ffmpeg():
    from app.services.final_assembly_service import ffmpeg
    return ffmpeg()


def _duration(path):
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError("Generated speaking clip has no video track")
        stream = container.streams.video[0]
        if stream.duration is not None:
            return float(stream.duration * stream.time_base)
        return float(container.duration or 0) / av.time_base


def _audio_url(shot):
    if shot.get("dialogue_audio_key"):
        return storage_service.asset_url(shot["dialogue_audio_key"])
    from app.services.video_generation_service import fresh_url
    return fresh_url(shot.get("dialogue_audio_url") or "")


def apply(media, shot):
    """Mutate a seekable MP4 file to carry only approved speech plus silence."""
    url = _audio_url(shot)
    if urlsplit(url).scheme != "https":
        raise ValueError("Approved speech URL is unavailable; generated speech was not accepted")
    with tempfile.TemporaryDirectory() as folder:
        source, audio, target = (Path(folder) / name for name in ("source.mp4", "approved.wav", "locked.mp4"))
        media.seek(0)
        source.write_bytes(media.read())
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
            response.raise_for_status()
            size = 0
            with audio.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise ValueError("Approved speech exceeds the 32MB safety bound")
                    output.write(chunk)
        video_seconds = _duration(source)
        if not math.isfinite(video_seconds) or video_seconds <= 0:
            raise ValueError("Generated speaking clip has no measurable duration")
        from app.services.voice_generation_service import decoded_audio_duration
        speech_seconds = decoded_audio_duration(audio.read_bytes())
        if speech_seconds > video_seconds + .03:
            raise ValueError("Generated clip is shorter than the approved line; regenerate it instead of truncating speech")
        command = [_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(source), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                   "-c:v", "copy", "-af", "apad", "-t", f"{video_seconds:.6f}",
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)]
        done = subprocess.run(command, capture_output=True, timeout=300)
        if done.returncode or not target.exists():
            raise ValueError("Could not lock the approved dialogue soundtrack; generated speech was not accepted")
        body = target.read_bytes()
    media.seek(0); media.truncate(); media.write(body); media.seek(0)
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "video_duration_sec": video_seconds, "approved_audio_duration_sec": speech_seconds,
            "policy": "approved-dialogue-plus-silence-v1"}
