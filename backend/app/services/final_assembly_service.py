"""Module U: deliberate local stitching, preserving source audio; no provider generation."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit

import av
import httpx

from app.services import job_service, storage_service

FPS = 30
FADE_SECONDS = 0.5  # Assembly currently supplies type, not duration. Symmetric A/V overlap.
MAX_TOTAL_BYTES = 2 * 1024 ** 3


class AssemblyError(ValueError):
    pass


def fingerprint(result, aspect_ratio, quality):
    def identity(shot):
        url = urlsplit(shot.get("video_url") or "")
        return [shot.get("shot_number"), shot.get("video_key") or urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                shot.get("video_sha256"), shot.get("video_task_id"), shot.get("video_status"), shot.get("video_source_changed", False)]
    value = [sorted([identity(s) for s in result.get("shots", [])], key=lambda s: s[0]),
             result.get("assembly", {}), result.get("audio_assembly_pending", False), aspect_ratio, quality]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare(job, result):
    """No network, disk media access, or FFmpeg before ALL shot videos pass this gate."""
    shots = sorted(result.get("shots", []), key=lambda s: s["shot_number"])
    if not shots:
        raise AssemblyError("Cannot assemble final video: the job has no shots.")
    missing = [s["shot_number"] for s in shots if not s.get("video_url")]
    if missing:
        raise AssemblyError("Cannot assemble final video: missing video for shot(s) " + ", ".join(map(str, missing)) + ". Generate or regenerate those shots first.")
    unfinished = [s["shot_number"] for s in shots if s.get("video_status") != "done" or s.get("video_source_changed")]
    if unfinished:
        raise AssemblyError("Cannot assemble final video: unfinished or outdated video for shot(s) " + ", ".join(map(str, unfinished)) + ".")
    if job.status != "done" or result.get("audio_assembly_pending") or result.get("assembly", {}).get("provisional"):
        raise AssemblyError("Finish final shot planning and audio approval before assembling the final video.")
    numbers = [s["shot_number"] for s in shots]
    if len(set(numbers)) != len(numbers):
        raise AssemblyError("Duplicate shot numbers; cannot determine the final timeline.")
    if len(shots) > 64:
        raise AssemblyError("This assembly supports at most 64 shots per job.")
    boundaries = {}
    for item in result.get("assembly", {}).get("transitions", []):
        between = item.get("between")
        if between in boundaries:
            raise AssemblyError(f"Duplicate Assembly transition for {between}.")
        boundaries[between] = item.get("type")
    transitions = []
    for left, right in zip(numbers, numbers[1:]):
        key = f"{left}-{right}"
        kind = boundaries.get(key)
        if kind not in {"cut", "match cut", "crossfade"}:
            raise AssemblyError(f"Missing or unsupported Assembly transition for shots {key}: {kind!r}.")
        transitions.append({"between": key, "type": kind})
    return {"source_hash": fingerprint(result, job.aspect_ratio, job.quality),
            "aspect_ratio": job.aspect_ratio, "quality": job.quality, "transitions": transitions,
            "shots": [{k: s.get(k) for k in ("shot_number", "video_url", "video_key", "video_sha256", "video_provider", "has_dialogue")} for s in shots]}


def ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path):
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise AssemblyError("Video track is missing.")
        stream = container.streams.video[0]
        rate = float(stream.average_rate or FPS)
        end, count = 0.0, 0
        for frame in container.decode(video=0):
            end = max(end, float(frame.time or 0) + (float(frame.duration * frame.time_base) if frame.duration else 1 / rate))
            count += 1
        dimensions = [stream.width, stream.height]
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise AssemblyError("Audio track is missing; cannot preserve the shot's audio.")
        audio_end, samples = 0.0, 0
        for frame in container.decode(audio=0):
            audio_end = max(audio_end, float(frame.time or 0) + frame.samples / frame.sample_rate)
            samples += frame.samples
    if not count or not samples or not math.isfinite(end) or end <= 0:
        raise AssemblyError("Empty or invalid media track.")
    return {"video_duration": end, "audio_duration": audio_end, "frames": count, "audio_samples": samples, "dimensions": dimensions}


def filter_graph(plan, measured):
    short = 480 if plan["quality"] == "480p" else 720
    w, h = map(int, plan["aspect_ratio"].split(":"))
    width, height = (2 * round(short * w / h / 2), short) if w >= h else (short, 2 * round(short * h / w / 2))
    durations = [math.ceil(max(m["video_duration"], m["audio_duration"]) * FPS - 1e-6) / FPS for m in measured]
    parts = []
    for i, duration in enumerate(durations):
        parts.append(f"[{i}:v:0]fps={FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.35,trim=duration={duration:.9f}[v{i}]")
        parts.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.9f}[a{i}]")
    v, a, total = 'v0', 'a0', durations[0]
    timeline = [{"shot_number": plan['shots'][0]['shot_number'], "start": 0.0, "duration": durations[0]}]
    boundaries = []
    for i, transition in enumerate(plan['transitions'], 1):
        nv, na = f'joinedv{i}', f'joineda{i}'
        fade = 0.0
        if transition['type'] == 'crossfade':
            # Quantize both media overlaps identically; adjacent dissolves cannot overlap each other.
            fade = math.floor(min(FADE_SECONDS, durations[i-1] / 2, durations[i] / 2) * FPS) / FPS
            if fade < 1 / FPS:
                raise AssemblyError(f"Clips too short for crossfade at {transition['between']}.")
            offset = total - fade
            parts.append(f"[{v}][v{i}]xfade=transition=fade:duration={fade:.9f}:offset={offset:.9f}[{nv}]")
            parts.append(f"[{a}][a{i}]acrossfade=d={fade:.9f}:c1=tri:c2=tri[{na}]")
        else:
            parts.append(f"[{v}][{a}][v{i}][a{i}]concat=n=2:v=1:a=1[{nv}][{na}]")
        start = total - fade
        boundaries.append({**transition, 'start': start, 'overlap_sec': fade})
        timeline.append({'shot_number': plan['shots'][i]['shot_number'], 'start': start, 'duration': durations[i]})
        total += durations[i] - fade
        parts.append(f"[{nv}]fps={FPS},settb=AVTB[stablev{i}]")
        v, a = f"stablev{i}", na
    return ';'.join(parts), v, a, {"duration": total, "shots": timeline, "boundaries": boundaries, "dimensions": [width, height], "fps": FPS, "audio_rate": 48000}


def render(paths, target, plan, emit=lambda note: None):
    measured = []
    for shot, path in zip(plan['shots'], paths):
        try:
            media = probe(path)
        except Exception as error:
            raise AssemblyError(f"Shot {shot['shot_number']}: unusable source media ({error}).") from error
        if abs(media['video_duration'] - media['audio_duration']) > 0.25:
            raise AssemblyError(f"Shot {shot['shot_number']}: source audio/video durations differ by more than 250ms; review the source before stitching.")
        measured.append(media)
    graph, v, a, timeline = filter_graph(plan, measured)
    command = [ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-filter_complex_threads', '1']
    for path in paths:
        command += ['-i', str(path)]
    command += ['-filter_complex', graph, '-map', f'[{v}]', '-map', f'[{a}]', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(target)]
    for attempt in range(2):
        try:
            run = subprocess.run(command, capture_output=True, timeout=900)
            if run.returncode:
                raise AssemblyError('FFmpeg failed: ' + run.stderr.decode(errors='replace')[-1500:])
            final = probe(target)
            if any(abs(final[k] - timeline['duration']) > 0.10 for k in ('video_duration', 'audio_duration')):
                raise AssemblyError('Stitched audio/video duration failed the 100ms timeline check.')
            return {'timeline': timeline, 'sources': measured, 'final': final, 'filter_graph': graph, 'local_attempts': attempt + 1}
        except (AssemblyError, subprocess.TimeoutExpired):
            if attempt:
                raise
            emit('Local stitch verification failed; retrying FFmpeg once. No media provider or TTS calls are made.')


def run(db, job_id, token, plan):
    """Run only from the explicit assemble action; never called by per-shot regeneration."""
    def emit(note):
        job_service.append_event(db, job_id, 'final_assembly', note)
    try:
        emit('Downloading all accepted shot videos in shot-number order; preserving their existing audio.')
        with tempfile.TemporaryDirectory() as folder:
            paths, total = [], 0
            for shot in plan['shots']:
                number = shot['shot_number']
                if urlsplit(shot['video_url']).scheme != 'https':
                    raise AssemblyError(f'Shot {number}: video URL must use HTTPS.')
                path = Path(folder) / f'shot-{number}.mp4'
                digest = hashlib.sha256()
                with httpx.stream('GET', shot['video_url'], timeout=120, follow_redirects=True) as response:
                    response.raise_for_status()
                    with path.open('wb') as output:
                        size = 0
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk); total += len(chunk)
                            if size > 512 * 1024 ** 2 or total > MAX_TOTAL_BYTES:
                                raise AssemblyError('Source media exceeded assembly download limits.')
                            output.write(chunk); digest.update(chunk)
                if shot.get('video_sha256') and digest.hexdigest() != shot['video_sha256']:
                    raise AssemblyError(f'Shot {number}: downloaded video hash does not match the accepted video.')
                paths.append(path)
            emit('Stitching hard cuts and paired video/audio crossfades. No grading or audio regeneration.')
            target = Path(folder) / 'final.mp4'
            evidence = render(paths, target, plan, emit)
            current_job = job_service.get_job(db, job_id)
            current = job_service.job_result(current_job)
            if fingerprint(current, current_job.aspect_ratio, current_job.quality) != plan['source_hash']:
                raise AssemblyError('Shot videos or Assembly transitions changed during stitching. Assemble again when all shots are ready.')
            key = f'jobs/{job_id}/final/{token}.mp4'
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            with target.open('rb') as media:
                stored = storage_service.upload_file(key, media, content_type='video/mp4')
            job_service.finish_final_assembly(db, job_id, token, status='done', key=key, url=stored['url'], sha256=digest, evidence=evidence)
            emit(f"Final video stored in S3: {key}; verified timeline {evidence['timeline']['duration']:.3f}s with video and audio tracks.")
    except Exception as error:
        db.rollback()
        message = str(error) if isinstance(error, AssemblyError) else f'Final assembly failed ({type(error).__name__}); source shots remain available.'
        job_service.finish_final_assembly(db, job_id, token, status='failed', error=message)
        emit('WARNING: ' + message)
