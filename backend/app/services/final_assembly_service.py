"""Module U: deliberate local stitching, preserving source audio; no provider generation."""
import hashlib
import json
import math
import re
from statistics import median, mean
from pathlib import Path
import shutil
import subprocess
import tempfile
from array import array
from urllib.parse import urlsplit, urlunsplit

import av
import httpx

from app.services import job_service, storage_service

FPS = 30
FADE_SECONDS = 0.5  # Assembly currently supplies type, not duration. Symmetric A/V overlap.
MAX_TOTAL_BYTES = 2 * 1024 ** 3


class AssemblyError(ValueError):
    pass


def fingerprint(result, aspect_ratio, quality, color_grade='None'):
    def identity(shot):
        url = urlsplit(shot.get("video_url") or "")
        return [shot.get("shot_number"), shot.get("video_key") or urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                shot.get("video_sha256"), shot.get("video_task_id"), shot.get("video_status"), shot.get("video_source_changed", False)]
    value = [sorted([identity(s) for s in result.get("shots", [])], key=lambda s: s[0]),
             result.get("assembly", {}), result.get("audio_assembly_pending", False), aspect_ratio, quality, color_grade or 'None']
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare(job, result, *, audio_offsets=None):
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
        boundaries[between] = item.get('type')
    # Explicit caller input ONLY. Never consume offsets from raw model output.
    # No production caller supplies this argument; dialogue timing stays unchanged.
    offsets = {} if audio_offsets is None else audio_offsets
    valid_pairs = {f'{left}-{right}' for left, right in zip(numbers, numbers[1:])}
    if not isinstance(offsets, dict) or any(key not in valid_pairs for key in offsets):
        raise AssemblyError('Explicit audio_offsets must map existing adjacent shot pairs to milliseconds.')
    transitions = []
    for left, right in zip(numbers, numbers[1:]):
        key = f"{left}-{right}"
        kind = boundaries.get(key)
        if kind not in {"cut", "match cut", "crossfade"}:
            raise AssemblyError(f"Missing or unsupported Assembly transition for shots {key}: {kind!r}.")
        transition = {"between": key, "type": kind}
        offset = offsets.get(key, 0)
        validate_audio_offset(kind, offset)
        if offset:
            transition['audio_offset_ms'] = offset
        transitions.append(transition)
    grade = getattr(job, 'color_grade', 'None') or 'None'
    if grade not in CREATIVE_GRADES:
        raise AssemblyError(f'Unsupported color grade: {grade!r}.')
    return {"source_hash": fingerprint(result, job.aspect_ratio, job.quality, grade), "color_grade": grade,
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


def validate_audio_offset(kind, offset):
    # Only prepare's separate keyword argument can opt into these plan values.
    if type(offset) is not int or (offset and (kind != 'cut' or not 200 <= abs(offset) <= 500)):
        raise AssemblyError('audio_offset_ms must be 0, or an integer from -500 to -200 (J) '
                            'or 200 to 500 (L), on a cut transition only.')


def verify_offset_audio(paths, plan, measured, emit):
    """Check actual decoded sound, not merely the existence of audio streams."""
    evidence, cache = [], {}
    def pcm(index):
        if index not in cache:
            result = subprocess.run([ffmpeg(), '-nostdin', '-v', 'error', '-i', str(paths[index]),
                                     '-vn', '-ac', '1', '-ar', '48000', '-f', 's16le', '-'],
                                    capture_output=True, timeout=900)
            if result.returncode:
                raise AssemblyError('Could not verify audio for the requested J/L-cut.')
            samples = array('h'); samples.frombytes(result.stdout)
            cache[index] = samples
        return cache[index]
    for i, transition in enumerate(plan['transitions'], 1):
        offset = transition.get('audio_offset_ms', 0)
        validate_audio_offset(transition['type'], offset)
        if not offset:
            continue
        seconds = abs(offset) / 1000
        if min(measured[i-1]['audio_duration'], measured[i]['audio_duration']) <= seconds * 2:
            raise AssemblyError(f"Shots {transition['between']}: clips too short for requested audio offset.")
        left, right = pcm(i-1), pcm(i)
        count = round(seconds * 48000)
        def dbfs(samples):
            rms = math.sqrt(sum(float(x) ** 2 for x in samples) / max(1, len(samples))) / 32768
            return 20 * math.log10(max(rms, 1e-12))
        levels = [dbfs(left[-count:]), dbfs(right[:count])]
        if min(levels) < -50 or hashlib.sha256(left.tobytes()).digest() == hashlib.sha256(right.tobytes()).digest():
            raise AssemblyError(f"Shots {transition['between']}: J/L-cut requires distinct audio with both boundary windows above -50 dBFS; measured {levels}.")
        evidence.append({'between': transition['between'], 'audio_offset_ms': offset, 'boundary_dbfs': levels})
        emit(f"Explicit {'J' if offset < 0 else 'L'}-cut at {transition['between']}: {abs(offset)}ms; "
             f"boundary audio levels {levels} dBFS. Video cut unchanged. WARNING: existing audio is shifted "
             "without additional source handles; on-camera dialogue sync may change. Review before use.")
    return evidence


def filter_graph(plan, measured):
    short = 480 if plan["quality"] == "480p" else 720
    w, h = map(int, plan["aspect_ratio"].split(":"))
    width, height = (2 * round(short * w / h / 2), short) if w >= h else (short, 2 * round(short * h / w / 2))
    durations = [math.ceil(max(m["video_duration"], m["audio_duration"]) * FPS - 1e-6) / FPS for m in measured]
    parts = []
    for i, duration in enumerate(durations):
        # FFmpeg 7 setpts clears link frame_rate (1/0), even for CFR sources.
        # Restore explicit CFR after all timestamp/padding/trim filters, before xfade.
        parts.append(f"[{i}:v:0]fps={FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.35,trim=duration={duration:.9f},fps={FPS},settb=AVTB[v{i}]")
        parts.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.9f}[a{i}]")
    v, a, total = 'v0', 'a0', durations[0]
    timeline = [{"shot_number": plan['shots'][0]['shot_number'], "start": 0.0, "duration": durations[0]}]
    boundaries = []
    for i, transition in enumerate(plan['transitions'], 1):
        offset_ms = transition.get('audio_offset_ms', 0)
        validate_audio_offset(transition['type'], offset_ms)
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
        elif offset_ms:
            # Visual hard cut is unchanged. Only the audio edit straddles it.
            # J advances incoming audio and pads its vacated end; L delays only
            # the outgoing shot (not earlier shots) and pads its vacated start.
            delta = abs(offset_ms) / 1000
            # Keep U's combined concat as the video clock: video-only concat
            # can cut earlier when decoded video/audio endpoints differ.
            parts.append(f"[{a}]asplit=2[clockleft{i}][editleft{i}]")
            parts.append(f"[a{i}]asplit=2[clockright{i}][editright{i}]")
            parts.append(f"[{v}][clockleft{i}][v{i}][clockright{i}]concat=n=2:v=1:a=1[{nv}][clockaudio{i}]")
            if offset_ms < 0:
                parts.append(f"[editright{i}]apad,atrim=duration={durations[i]+delta:.9f}[jpad{i}]")
                parts.append(f"[editleft{i}][jpad{i}]acrossfade=d={delta:.9f}:c1=tri:c2=tri[editedaudio{i}]")
            else:
                prefix = total - durations[i-1]
                if prefix > 0:
                    parts.append(f"[editleft{i}]asplit=2[lprefix{i}][ltail{i}]")
                    parts.append(f"[lprefix{i}]atrim=end={prefix:.9f},asetpts=PTS-STARTPTS[lp{i}]")
                    parts.append(f"[ltail{i}]atrim=start={prefix:.9f},asetpts=PTS-STARTPTS,adelay={offset_ms}:all=1[lt{i}]")
                    parts.append(f"[lp{i}][lt{i}]concat=n=2:v=0:a=1[ldelay{i}]")
                else:
                    parts.append(f"[editleft{i}]adelay={offset_ms}:all=1[ldelay{i}]")
                parts.append(f"[ldelay{i}][editright{i}]acrossfade=d={delta:.9f}:c1=tri:c2=tri[editedaudio{i}]")
            # Keep the clock branch connected to a demanded output. An anullsink
            # here can trigger FFmpeg 7's best_input scheduling assertion.
            parts.append(f"[clockaudio{i}][editedaudio{i}]amix=inputs=2:weights='0 1':normalize=0:duration=longest[{na}]")
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
    offset_evidence = verify_offset_audio(paths, plan, measured, emit)
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
            return {'timeline': timeline, 'sources': measured, 'final': final, 'filter_graph': graph,
                    'audio_offsets': offset_evidence, 'local_attempts': attempt + 1}
        except (AssemblyError, subprocess.TimeoutExpired):
            if attempt:
                raise
            emit('Local stitch verification failed; retrying FFmpeg once. No media provider or TTS calls are made.')


# Module W uses reproducible filter recipes, not third-party LUT assets.
CREATIVE_GRADES = {
    'None': '',
    'Warm': 'colorbalance=rm=0.035:bm=-0.025:pl=1,eq=saturation=1.04',
    'Cool': 'colorbalance=rm=-0.025:bm=0.035:pl=1,eq=saturation=0.98',
    'Vintage': 'colorbalance=rm=0.025:bm=-0.025:pl=1,eq=saturation=0.78:contrast=0.94:brightness=0.015',
    'Neon': 'colorbalance=rs=0.025:bs=0.04:pl=1,vibrance=intensity=0.25,eq=contrast=1.06',
    'Black & white': 'hue=s=0',
    'Vibrant': 'vibrance=intensity=0.2,eq=saturation=1.12:contrast=1.04',
}


def analyze_color(path, timeline):
    """Analyze the stitched timeline, excluding blended boundaries from shot averages."""
    with tempfile.TemporaryDirectory() as folder:
        cmd = [ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(Path(path).resolve()),
               '-an', '-vf', 'scale=160:-2,format=yuv420p,signalstats,metadata=print:file=stats.txt', '-f', 'null', '-']
        result = subprocess.run(cmd, cwd=folder, capture_output=True, timeout=900)
        if result.returncode:
            raise AssemblyError('Color analysis failed: ' + result.stderr.decode(errors='replace')[-1000:])
        frames = []
        for line in (Path(folder) / 'stats.txt').read_text().splitlines():
            match = re.search(r'pts_time:([0-9.eE+-]+)', line)
            if match:
                frames.append({'time': float(match[1])})
            elif frames and line.startswith('lavfi.signalstats.'):
                key, value = line.split('=', 1)
                if key.rsplit('.', 1)[-1] in {'YAVG', 'UAVG', 'VAVG', 'SATAVG'}:
                    frames[-1][key.rsplit('.', 1)[-1]] = float(value)
    shots = []
    for i, shot in enumerate(timeline['shots']):
        start = shot['start'] + (timeline['boundaries'][i-1]['overlap_sec'] if i else 0)
        end = shot['start'] + shot['duration'] - (timeline['boundaries'][i]['overlap_sec'] if i < len(timeline['boundaries']) else 0)
        selected = [f for f in frames if start <= f['time'] < end]
        if not selected:
            raise AssemblyError(f"No unblended color samples for shot {shot['shot_number']}.")
        values = {k: mean(f[k] for f in selected) for k in ('YAVG', 'UAVG', 'VAVG', 'SATAVG')}
        shots.append({**shot, **values, 'samples': len(selected), 'analysis_start': start, 'analysis_end': end})
    return shots


def correction_plan(stats):
    # Image averages are content-dependent proxies, NOT measured Kelvin/white balance.
    # Intentional scene color can look like drift. Conservative caps limit that risk;
    # this does not recover clipped detail or replace a colorist's review.
    baseline = {k: median(s[k] for s in stats) for k in ('YAVG', 'UAVG', 'VAVG', 'SATAVG')}
    clamp = lambda v, limit: max(-limit, min(limit, v))
    adjustments = []
    for shot in stats:
        dy = baseline['YAVG'] - shot['YAVG']
        du, dv = baseline['UAVG'] - shot['UAVG'], baseline['VAVG'] - shot['VAVG']
        ratio = baseline['SATAVG'] / max(shot['SATAVG'], 1)
        saturation = 1 + clamp((ratio - 1) * .7, .15) if abs(ratio - 1) > .20 else 1
        chroma_outlier = math.hypot(du, dv) > 6
        adjustments.append({'shot_number': shot['shot_number'],
                            'y': clamp(dy * .7, 16) if abs(dy) > 10 else 0,
                            'u': clamp(baseline['UAVG'] - (128 + (shot['UAVG'] - 128) * saturation), 6) if chroma_outlier else 0,
                            'v': clamp(baseline['VAVG'] - (128 + (shot['VAVG'] - 128) * saturation), 6) if chroma_outlier else 0,
                            'saturation': saturation})
    return baseline, adjustments


def correction_filter(timeline, adjustments):
    """Blend correction coefficients during existing dissolves; never edit the transition."""
    terms = {k: [] for k in ('y', 'u', 'v', 'saturation')}
    for i, (shot, adj) in enumerate(zip(timeline['shots'], adjustments)):
        start, end = shot['start'], shot['start'] + shot['duration']
        incoming = timeline['boundaries'][i-1]['overlap_sec'] if i else 0
        outgoing = timeline['boundaries'][i]['overlap_sec'] if i < len(timeline['boundaries']) else 0
        weight = f'gte(T,{start:.9f})*lt(T,{end:.9f})'
        if incoming:
            weight += f'*clip((T-{start:.9f})/{incoming:.9f},0,1)'
        if outgoing:
            weight += f'*clip(({end:.9f}-T)/{outgoing:.9f},0,1)'
        for key in terms:
            value = adj[key] - (1 if key == 'saturation' else 0)
            if value:
                terms[key].append(f'({value:.9f})*({weight})')
    if not any(terms.values()):
        return ''
    values = {k: '+'.join(v) or '0' for k, v in terms.items()}
    sat = f"(1+({values['saturation']}))"
    return (f"geq=lum='clip(lum(X,Y)+({values['y']}),0,255)':"
            f"cb='clip(128+(cb(X,Y)-128)*{sat}+({values['u']}),0,255)':"
            f"cr='clip(128+(cr(X,Y)-128)*{sat}+({values['v']}),0,255)'")


def color_encode(source, target, filters, emit):
    if not filters:
        shutil.copyfile(source, target)
        return
    cmd = [ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
           '-vf', filters, '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'fast',
           '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(target)]
    original = probe(source)
    for attempt in range(2):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=900)
            if result.returncode:
                raise AssemblyError('Color pass failed: ' + result.stderr.decode(errors='replace')[-1000:])
            actual = probe(target)
            if actual['frames'] != original['frames'] or actual['audio_samples'] != original['audio_samples'] or any(
                    abs(actual[k] - original[k]) > .05 for k in ('video_duration', 'audio_duration')):
                raise AssemblyError('Color pass changed timeline or audio; refusing the result.')
            return
        except (AssemblyError, subprocess.TimeoutExpired):
            if attempt:
                raise
            emit('WARNING: Color-pass verification failed; retrying the local pass once.')


def apply_color_pipeline(source, target, timeline, grade, emit=lambda note: None):
    if grade not in CREATIVE_GRADES:
        raise AssemblyError(f'Unsupported color grade: {grade!r}.')
    before = analyze_color(source, timeline)
    baseline, adjustments = correction_plan(before)
    filters = correction_filter(timeline, adjustments)
    emit('Technical color correction: bounded sequence-median normalization on the stitched timeline. '
         'Image statistics are content-dependent; intentional scene color may also be affected. '
         + json.dumps(adjustments))
    with tempfile.TemporaryDirectory() as folder:
        corrected = Path(folder) / 'corrected.mp4'
        color_encode(source, corrected, filters, emit)
        after = analyze_color(corrected, timeline)
        emit(f'Technical correction complete. Applying uniform creative grade: {grade}.'
             + (' Creative pass is a byte-for-byte copy.' if grade == 'None' else ''))
        color_encode(corrected, target, CREATIVE_GRADES[grade], emit)
        corrected_hash = hashlib.sha256(corrected.read_bytes()).hexdigest()
    return {'baseline': baseline, 'before': before, 'corrected': after, 'adjustments': adjustments,
            'correction_filter': filters, 'grade': grade, 'grade_filter': CREATIVE_GRADES[grade],
            'corrected_sha256': corrected_hash, 'final_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'graded': analyze_color(target, timeline)}


def apply_deflicker(source, target, timeline, emit=lambda note: None):
    """Module AH: transition-isolated windows AFTER grading; audio is stream-copied.

    FFmpeg defaults (five frames, arithmetic mean). This normalizes luminance,
    not arbitrary chromatic flicker or motion. Already-blended crossfade frames
    have no single-shot ownership: bypass them, isolating both surrounding shots.
    No transitions are recreated, and no source shots are processed pre-stitch.
    """
    filters = 'deflicker=size=5:mode=am'
    original = probe(source)
    fps = timeline['fps']
    edges = {0, original['frames']}
    fades = []
    for boundary in timeline['boundaries']:
        start = max(0, min(original['frames'], round(boundary['start'] * fps)))
        end = max(start, min(original['frames'], round((boundary['start'] + boundary['overlap_sec']) * fps)))
        edges.update((start, end))
        if end > start:
            fades.append((start, end))
    edges = sorted(edges)
    segments = [{'start_frame': start, 'end_frame': end,
                 'filtered': end - start >= 5 and not any(a <= start < b for a, b in fades)}
                for start, end in zip(edges, edges[1:])]
    parts = ['[0:v:0]split=' + str(len(segments)) + ''.join(f'[s{i}]' for i in range(len(segments)))]
    for i, segment in enumerate(segments):
        parts.append(f"[s{i}]trim=start_frame={segment['start_frame']}:end_frame={segment['end_frame']},"
                     'setpts=PTS-STARTPTS' + (',' + filters if segment['filtered'] else '') + f'[d{i}]')
    parts.append(''.join(f'[d{i}]' for i in range(len(segments)))
                 + f'concat=n={len(segments)}:v=1:a=0,fps={fps}[deflickered]')
    graph = ';'.join(parts)
    command = [ffmpeg(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
               '-filter_complex', graph, '-map', '[deflickered]', '-map', '0:a:0', '-c:v', 'libx264',
               '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy',
               '-movflags', '+faststart', str(target)]
    emit('Creative grading complete. Starting transition-isolated final timeline deflicker: ' + filters
         + '; audio stream-copy. Crossfade intervals and spans shorter than five frames bypassed. '
         + json.dumps(segments))
    for attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, timeout=900)
            if result.returncode:
                raise AssemblyError('Deflicker failed: ' + result.stderr.decode(errors='replace')[-1000:])
            actual = probe(target)
            if (actual['frames'] != original['frames'] or actual['dimensions'] != original['dimensions']
                    or actual['audio_samples'] != original['audio_samples'] or any(
                        abs(actual[k] - original[k]) > .05 for k in ('video_duration', 'audio_duration'))):
                raise AssemblyError('Deflicker changed timeline, dimensions or audio; refusing the result.')
            emit('Final timeline deflicker complete; frame count, dimensions and audio duration verified. Ready for S3 storage.')
            return {'filter': filters, 'filter_graph': graph, 'segments': segments,
                    'before': original, 'after': actual, 'audio': 'stream-copy',
                    'local_attempts': attempt + 1, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'final_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
        except (AssemblyError, subprocess.TimeoutExpired):
            if attempt:
                raise
            emit('WARNING: Deflicker verification failed; retrying the local pass once.')


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
            emit('Stitching hard cuts and paired video/audio crossfades before technical correction and creative grading.')
            target = Path(folder) / 'final.mp4'
            graded = Path(folder) / 'graded.mp4'
            stitched = Path(folder) / 'stitched.mp4'
            evidence = render(paths, stitched, plan, emit)
            evidence['stitched_final'] = evidence['final']
            evidence['color_pipeline'] = apply_color_pipeline(stitched, graded, evidence['timeline'], plan['color_grade'], emit)
            evidence['deflicker'] = apply_deflicker(graded, target, evidence['timeline'], emit)
            evidence['final'] = probe(target)
            current_job = job_service.get_job(db, job_id)
            current = job_service.job_result(current_job)
            if fingerprint(current, current_job.aspect_ratio, current_job.quality, current_job.color_grade) != plan['source_hash']:
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
