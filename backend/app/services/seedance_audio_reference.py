"""Validate EvoLink Seedance and fal H3 Max audio assets before a paid generation submission.

Their reference contracts require 2–15 second WAV/MP3 clips. Short speech is
preserved at its original speed; only trailing silence is added to a COPY.
The approved speech asset, its duration and the requested video stay unchanged.
"""
import hashlib
import io
import math
import wave
from types import SimpleNamespace

import av
import httpx

from app.services import storage_service

MAX_BYTES = 15_000_000
MIN_SECONDS = 3.0  # Product policy; safely above EvoLink's two-second minimum.
PAD_SECONDS = 3.0  # Original dialogue followed by silence, never another line.
MAX_SECONDS = 15.0


def _decoded_pcm(data, expected_duration):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("The speech reference is empty or exceeds the 15 MB reference limit. Recreate this shot's speech.")
    try:
        with wave.open(io.BytesIO(data), 'rb') as source:
            params = source.getparams()
            pcm = source.readframes(params.nframes)
        frame_bytes = params.nchannels * params.sampwidth
        if len(pcm) != params.nframes * frame_bytes:
            raise ValueError("The speech file is incomplete. Recreate this shot's speech before generating video.")
        duration = params.nframes / params.framerate
    except (wave.Error, EOFError):
        # MP3 is also accepted. Decode to measure real samples, not a stored
        # metadata duration; convert only if this asset needs silence padding.
        try:
            with av.open(io.BytesIO(data)) as source:
                if source.format.name != 'mp3':
                    raise ValueError('unsupported audio format')
                resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
                chunks = []
                samples = 0
                for frame in source.decode(audio=0):
                    for converted in resampler.resample(frame):
                        samples += converted.samples
                        if samples > 24000 * MAX_SECONDS:
                            raise ValueError('audio too long')
                        chunks.append(converted.to_ndarray().tobytes())
                for converted in resampler.resample(None):
                    samples += converted.samples
                    chunks.append(converted.to_ndarray().tobytes())
                pcm = b''.join(chunks)
                params = SimpleNamespace(nchannels=1, sampwidth=2, framerate=24000, nframes=samples)
                duration = samples / 24000
        except Exception as error:
            raise ValueError("The speech reference cannot be decoded as a supported WAV/MP3 of at most 15 seconds. Recreate this shot's speech.") from error
    if not 0 < duration <= MAX_SECONDS:
        raise ValueError("The video model needs reference speech of at most 15 seconds. Split longer dialogue into complete lines.")
    expected = float(expected_duration)
    if not math.isfinite(expected) or abs(expected - duration) > 0.05:
        raise ValueError("The speech file does not match its saved duration. Recreate this shot's speech before generating video.")
    return pcm, params, duration, {'source_duration_sec': duration,
        'reference_duration_sec': duration, 'leading_silence_sec': 0,
        'trailing_silence_sec': 0, 'source_sha256': hashlib.sha256(data).hexdigest()}


def inspect_and_pad(data, expected_duration):
    pcm, params, duration, evidence = _decoded_pcm(data, expected_duration)
    if duration >= MIN_SECONDS:
        return None, evidence
    target_frames = math.ceil(PAD_SECONDS * params.framerate)
    # WAV 8-bit PCM silence is unsigned midpoint; all other PCM is zero.
    silence = b'\x80' if params.sampwidth == 1 else b'\x00'
    pcm += silence * ((target_frames - params.nframes) * params.nchannels * params.sampwidth)
    output = io.BytesIO()
    with wave.open(output, 'wb') as padded:
        padded.setparams((params.nchannels, params.sampwidth, params.framerate, 0, 'NONE', 'not compressed'))
        padded.writeframes(pcm)
    evidence.update(reference_duration_sec=target_frames / params.framerate,
                    trailing_silence_sec=(target_frames - params.nframes) / params.framerate)
    return output.getvalue(), evidence


def align_exact_audio(data, expected_duration, start_sec, video_duration):
    """Preserve every approved sample and place it once on a full-shot silent bed."""
    pcm, params, duration, evidence = _decoded_pcm(data, expected_duration)
    start, total = float(start_sec), float(video_duration)
    if not all(math.isfinite(value) for value in (start, total)) or start < 0 or not 5 <= total <= 15:
        raise ValueError("The exact-audio shot needs a valid 5–15 second speaking window.")
    first = round(start * params.framerate)
    frames = len(pcm) // (params.nchannels * params.sampwidth)
    last = round(total * params.framerate)
    if first + frames > last:
        raise ValueError("The approved speech does not fit its directed window. Revise the shot before generating video.")
    silence = b'\x80' if params.sampwidth == 1 else b'\x00'
    frame_bytes = params.nchannels * params.sampwidth
    output = io.BytesIO()
    with wave.open(output, 'wb') as aligned:
        aligned.setparams((params.nchannels, params.sampwidth, params.framerate, 0, 'NONE', 'not compressed'))
        aligned.writeframes(silence * first * frame_bytes)
        aligned.writeframes(pcm)
        aligned.writeframes(silence * (last - first - frames) * frame_bytes)
    evidence.update(reference_duration_sec=last / params.framerate,
                    leading_silence_sec=first / params.framerate,
                    trailing_silence_sec=(last-first-frames) / params.framerate)
    return output.getvalue(), evidence


def prepare(translated, shot, job_id):
    """Called after the duplicate-submission claim, before any paid API call."""
    h3 = translated.get('audio_model') == 'h3_max_fal'
    if not h3 and (translated.get('provider') != 'evolink' or not translated.get('audio_model', '').startswith('seedance_')):
        return None
    request = translated['request']
    exact = h3 and 'target_audio_url' in request
    audio_key = 'target_audio_url' if exact else 'reference_audio_urls' if h3 else 'audio_urls'
    audio_url = request.get(audio_key) if exact else (request.get(audio_key) or [None])[0]
    if not isinstance(audio_url, str) or not audio_url:
        raise ValueError("This speaking-shot path requires exactly one approved speech reference.")
    try:
        with httpx.stream('GET', audio_url, timeout=60, follow_redirects=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("The speech reference exceeds the 15 MB reference limit. Recreate this shot's speech.")
                chunks.append(chunk)
        if exact:
            from app.services.dialogue_window import from_shot
            timing = from_shot({**shot, 'duration_sec': request['duration']}, require=True)
            padded, evidence = align_exact_audio(b''.join(chunks), shot['dialogue_audio_duration_sec'],
                                                  timing['start_sec'], request['duration'])
        else:
            padded, evidence = inspect_and_pad(b''.join(chunks), shot['dialogue_audio_duration_sec'])
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("We couldn't read the approved speech reference. No video request was sent; try generating this shot again.") from error
    if padded is not None:
        digest = hashlib.sha256(padded).hexdigest()
        key = (f'jobs/{job_id}/shots/{shot["shot_number"]}/'
               f'{"h3-exact-audio-v1" if exact else "seedance-reference-v1"}-{digest}.wav')
        try:
            asset = storage_service.upload_bytes(key, padded, content_type='audio/wav')
            # Provider references need enough URL lifetime for queue + processing.
            url = storage_service.asset_url(asset['key'], expires_in=86400)
            request[audio_key] = url if exact else [url]
        except Exception as error:
            raise ValueError("We couldn't prepare this short speech reference. No video request was sent; try again.") from error
        evidence['reference_key'] = key
    return evidence
