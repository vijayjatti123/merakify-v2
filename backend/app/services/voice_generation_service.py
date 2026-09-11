import asyncio
import base64
import uuid
import io
import json
import wave
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services import job_service, storage_service


SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
TTS_TIMEOUT_SECONDS = 35.0

LANGUAGE_CODES = {
    "english": "en-IN",
    "hindi": "hi-IN",
    "bengali": "bn-IN",
    "tamil": "ta-IN",
    "telugu": "te-IN",
    "kannada": "kn-IN",
    "malayalam": "ml-IN",
    "marathi": "mr-IN",
    "gujarati": "gu-IN",
    "punjabi": "pa-IN",
    "odia": "od-IN",
}

FEMALE_VOICE_IDS = frozenset(
    {"ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya", "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali"}
)
OLDER_TERMS = ("elderly", "older", "old woman", "old man", "grandmother", "grandfather", "senior")
YOUNGER_TERMS = ("child", "young girl", "young boy", "teen", "teenage", "daughter", "son")
FEMALE_TERMS = ("woman", "female", "girl", "mother", "daughter", "grandmother", "wife", "sister")
MALE_TERMS = ("man", "male", "boy", "father", "son", "grandfather", "husband", "brother")

# Public ElevenLabs catalog IDs used only as a technical fallback. Character
# identity remains the Sarvam catalog voice_id selected for the job.
ELEVENLABS_FEMALE_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
ELEVENLABS_MALE_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam


class VoiceGenerationError(RuntimeError):
    """Raised when both the primary and fallback voice providers fail."""


@dataclass(frozen=True)
class GeneratedAudio:
    data: bytes
    content_type: str
    extension: str
    provider: str


def decoded_audio_duration(data: bytes) -> float:
    """Measure actual decoded samples, never a provider duration field or text estimate."""
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            samples = audio.readframes(audio.getnframes())
            duration = len(samples) / (audio.getsampwidth() * audio.getnchannels() * audio.getframerate())
    except (wave.Error, EOFError):
        import soundfile
        samples, rate = soundfile.read(io.BytesIO(data), dtype="float32", always_2d=True)
        duration = len(samples) / rate
    if duration <= 0:
        raise VoiceGenerationError("Decoded audio has no duration")
    return duration


def heuristic_voice_id(description: str) -> str:
    """Choose one fixed Sarvam catalog voice from simple age/gender words."""
    text = description.casefold()
    is_female = any(term in text for term in FEMALE_TERMS)
    is_male = any(term in text for term in MALE_TERMS)
    is_older = any(term in text for term in OLDER_TERMS)
    is_younger = any(term in text for term in YOUNGER_TERMS)

    if is_female:
        return "roopa" if is_older else "kavya" if is_younger else "priya"
    if is_male:
        return "ratan" if is_older else "kabir" if is_younger else "rahul"
    return "shubh"


def assign_missing_voice_ids(continuity: dict, shots: list[dict] | None = None) -> int:
    """Assign stable catalog IDs once without replacing vault-selected voices."""
    assigned = 0
    for character in continuity.get("characters", []):
        voice_id = character.get("voice_id")
        if not voice_id:
            voice_id = heuristic_voice_id(character.get("description") or "")
            character["voice_id"] = voice_id
            character["voice_assignment"] = "heuristic"
            assigned += 1
        # voice_sample_ref is a Sarvam catalog ID, never a cloned voice sample.
        character["voice_sample_ref"] = voice_id

    narrator_used = bool(
        shots
        and any(shot.get("has_dialogue") and not shot.get("characters_in_shot") for shot in shots)
    )
    if narrator_used and not continuity.get("narrator_voice_ref"):
        continuity["narrator_voice_ref"] = heuristic_voice_id("neutral narrator")
        assigned += 1
    return assigned


def _language_code(language: str) -> str:
    code = LANGUAGE_CODES.get(language.strip().casefold())
    if not code:
        raise RuntimeError(f'Sarvam does not support configured language "{language}"')
    return code


def _provider_error(provider: str, response: httpx.Response) -> RuntimeError:
    detail = response.text.strip().replace("\n", " ")[:400]
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"{provider} TTS returned HTTP {response.status_code}{suffix}")


def _exception_detail(error: Exception) -> str:
    """Keep provider failures useful even when an exception has no message."""
    message = str(error).strip()
    error_type = type(error).__name__
    return f"{error_type}: {message}" if message else error_type


async def _sarvam_tts(
    client: httpx.AsyncClient,
    text: str,
    voice_id: str,
    language: str,
    pace: float = 1.0,
) -> GeneratedAudio:
    if len(text) > 2500:
        raise VoiceGenerationError("Dialogue exceeds Sarvam's 2,500-character limit")
    if not settings.sarvam_api_key.strip():
        raise RuntimeError("SARVAM_API_KEY is not configured")
    response = await client.post(
        SARVAM_TTS_URL,
        headers={"api-subscription-key": settings.sarvam_api_key},
        json={
            "text": text,
            "language_code": _language_code(language),
            "speaker": voice_id,
            "model": "bulbul:v3",
            "output_audio_codec": "wav",
            "pace": pace,
            "speech_sample_rate": 24000,
        },
    )
    if not response.is_success:
        raise _provider_error("Sarvam", response)
    payload = response.json()
    audios = payload.get("audios") or []
    if not audios:
        raise RuntimeError("Sarvam TTS returned no audio")
    try:
        audio = base64.b64decode(audios[0], validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("Sarvam TTS returned invalid base64 audio") from error
    if not audio:
        raise RuntimeError("Sarvam TTS returned empty audio")
    return GeneratedAudio(audio, "audio/wav", ".wav", "sarvam")


def _elevenlabs_voice_id(sarvam_voice_id: str) -> str:
    return ELEVENLABS_FEMALE_VOICE_ID if sarvam_voice_id in FEMALE_VOICE_IDS else ELEVENLABS_MALE_VOICE_ID


async def _elevenlabs_tts(
    client: httpx.AsyncClient,
    text: str,
    sarvam_voice_id: str,
) -> GeneratedAudio:
    if not settings.elevenlabs_api_key.strip():
        raise RuntimeError("ELEVENLABS_API_KEY is not configured")
    voice_id = _elevenlabs_voice_id(sarvam_voice_id)
    response = await client.post(
        f"{ELEVENLABS_TTS_URL}/{voice_id}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": settings.elevenlabs_api_key},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
    )
    if not response.is_success:
        raise _provider_error("ElevenLabs", response)
    if not response.content:
        raise RuntimeError("ElevenLabs TTS returned empty audio")
    return GeneratedAudio(response.content, "audio/mpeg", ".mp3", "elevenlabs")


async def synthesize_dialogue(
    client: httpx.AsyncClient,
    *,
    text: str,
    voice_id: str,
    language: str,
    pace: float = 1.0,
) -> GeneratedAudio:
    """Use Sarvam for every language; fall back only after its call fails."""
    from app.services.voice_timing import native_script_guard
    native_script_guard(text, language)
    try:
        return await _sarvam_tts(client, text, voice_id, language, pace=pace)
    except Exception as sarvam_error:  # noqa: BLE001 - provider failure activates fallback
        try:
            return await _elevenlabs_tts(client, text, voice_id)
        except Exception as elevenlabs_error:  # noqa: BLE001 - persist both provider failures
            raise VoiceGenerationError(
                f"Sarvam failed ({_exception_detail(sarvam_error)}); "
                f"ElevenLabs fallback failed ({_exception_detail(elevenlabs_error)})"
            ) from elevenlabs_error


def _shot_voice_id(shot: dict) -> str:
    voice_refs = shot.get("voice_refs") or {}
    for voice_id in voice_refs.values():
        if isinstance(voice_id, str) and voice_id.strip():
            return voice_id.strip()
    raise VoiceGenerationError("dialogue shot has no assigned character or narrator voice_id")


async def _generate_dialogue_shot(
    db: Session,
    job_id: str,
    shot: dict,
    language: str,
    client: httpx.AsyncClient,
    mood: str = "",
) -> None:
    shot_number = shot["shot_number"]
    voice_id = None
    try:
        # Contain the entire per-shot task so one malformed or failed shot
        # cannot cancel successful siblings awaited by asyncio.gather below.
        voice_id = _shot_voice_id(shot)
        job_service.update_shot_fields(
            db,
            job_id,
            shot_number,
            status="generating",
            error_message=None,
            dialogue_audio_url=None,
            dialogue_audio_provider=None,
            dialogue_voice_id=voice_id,
        )
        job_service.append_shot_status_event(
            db,
            job_id,
            shot_number,
            "generating",
            message=f"Generating dialogue audio for shot {shot_number} with voice {voice_id}.",
            dialogue_voice_id=voice_id,
        )
        from app.services.voice_timing import mood_pace
        text = shot.get("dialogue_text") or ""
        target = float(shot["duration_sec"])
        if target <= 0:
            raise VoiceGenerationError("Dialogue shot duration must be positive")
        # By design, expressive mood pace takes precedence over the fit-based
        # pace estimate used only to select a voice. That estimate is not a
        # synthesis setting; timing correction happens after decoding real audio.
        pace = mood_pace(mood)
        generated = await synthesize_dialogue(
            client,
            text=text,
            voice_id=voice_id,
            language=language,
            pace=pace,
        )
        measured = decoded_audio_duration(generated.data)
        attempts = [{"pace":pace,"duration_sec":measured,"provider":generated.provider}]
        mismatch = abs(measured - target) / target
        if .10 < mismatch <= .15 and generated.provider == "sarvam":
            corrected_pace = min(2.0, max(.5, pace * measured / target))
            job_service.append_event(db, job_id, "voice_generation", f"Shot {shot_number}: decoded {measured:.3f}s vs {target:.3f}s; one bounded pace retry {pace:.3f} -> {corrected_pace:.3f}.")
            # Exactly one retry; a retry failure retains the successful first audio.
            try:
                corrected = await _sarvam_tts(client, text, voice_id, language, pace=corrected_pace)
                corrected_duration = decoded_audio_duration(corrected.data)
                attempts.append({"pace":corrected_pace,"duration_sec":corrected_duration,"provider":corrected.provider})
                generated, measured, pace = corrected, corrected_duration, corrected_pace
            except Exception as error:
                job_service.append_event(db, job_id, "voice_generation", f"Shot {shot_number}: bounded pace retry failed ({type(error).__name__}); retaining the first decoded audio.")
        final_duration = measured if abs(measured-target)/target > .10 else target
        if final_duration != target:
            job_service.append_event(db, job_id, "voice_generation", f"Shot {shot_number}: duration_sec corrected {target:.3f}s -> decoded audio {measured:.3f}s; no further pace retries. Assembly/duration check follows audio correction.")
        if measured > 9:
            job_service.append_event(db, job_id, "voice_generation", f"Warning: shot {shot_number} audio is {measured:.3f}s, exceeding the current 9-second video-model limit; dialogue was preserved and requires review.")
        object_key = f"jobs/{job_id}/shots/{shot_number}/dialogue-{uuid.uuid4()}{generated.extension}"
        uploaded = await asyncio.to_thread(
            storage_service.upload_bytes,
            object_key,
            generated.data,
            content_type=generated.content_type,
            cache_control="private, max-age=3600",
        )
        fields = {
            "status": "done",
            "error_message": None,
            "dialogue_audio_url": uploaded["url"],
            "dialogue_audio_provider": generated.provider,
            "dialogue_voice_id": voice_id,
            "duration_sec": final_duration,
            "dialogue_audio_duration_sec": measured,
            "dialogue_timing": {"character_count":len(text),"planned_duration_sec":target,"mood":mood,
                                "mood_pace":mood_pace(mood),"final_pace":pace,"attempts":attempts,
                                "relative_error":abs(measured-final_duration)/final_duration},
        }
        job_service.update_shot_fields(db, job_id, shot_number, **fields)
        job_service.append_shot_status_event(
            db,
            job_id,
            shot_number,
            "done",
            message=f"Dialogue audio for shot {shot_number} completed via {generated.provider}.",
            **{key: value for key, value in fields.items() if key != "status"},
        )
    except Exception as error:  # noqa: BLE001 - persist the real provider failure on the shot
        error_message = str(error)[:1200]
        error_fields = {
            "status": "error",
            "error_message": error_message,
            "dialogue_audio_url": None,
            "dialogue_audio_provider": None,
        }
        if voice_id:
            error_fields["dialogue_voice_id"] = voice_id
        job_service.update_shot_fields(db, job_id, shot_number, **error_fields)
        job_service.append_shot_status_event(
            db,
            job_id,
            shot_number,
            "error",
            message=f"Dialogue audio for shot {shot_number} failed: {error_message}",
            error_message=error_message,
            **({"dialogue_voice_id": voice_id} if voice_id else {}),
        )


async def generate_job_dialogue_audio(
    db: Session,
    job_id: str,
    *,
    shot_numbers: set[int] | None = None,
) -> None:
    """Generate selected dialogue shots concurrently and persist real progress."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise LookupError("job not found")
    result = job_service.job_result(job)
    if not result:
        raise ValueError("job has no stored result")

    from app.services.voice_timing import select_calibrated_voices
    from app.agents.director import _attach_voice_refs, finalize_audio_assembly
    selections = select_calibrated_voices(db, result, job.language or "English")
    for selection in selections:
        job_service.append_event(db, job_id, "voice_generation", f"Calibrated voice selection: {json.dumps(selection)}")
    if selections:
        result["shots"] = _attach_voice_refs(result["shots"], result["continuity"]["characters"], result["continuity"].get("narrator_voice_ref"))
    has_dialogue = any(shot.get("has_dialogue") for shot in result.get("shots", []))
    result["audio_assembly_pending"] = has_dialogue
    job_service.set_result(db, job_id, result)
    moods = {scene["scene_number"]:scene.get("mood", "") for scene in result.get("script", {}).get("scenes", [])}

    selected = [
        shot
        for shot in result.get("shots", [])
        if shot_numbers is None or shot.get("shot_number") in shot_numbers
    ]
    dialogue_shots = []
    for shot in selected:
        if shot.get("has_dialogue"):
            dialogue_shots.append(shot)
            continue
        shot_number = shot["shot_number"]
        job_service.update_shot_fields(db, job_id, shot_number, status="done", error_message=None)
        job_service.append_shot_status_event(
            db,
            job_id,
            shot_number,
            "done",
            message=f"Shot {shot_number} has no dialogue audio to generate.",
        )

    timeout = httpx.Timeout(TTS_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await asyncio.gather(
            *(
                _generate_dialogue_shot(db, job_id, shot, job.language or "English", client, moods.get(shot.get("scene_number"), ""))
                for shot in dialogue_shots
            )
        )

    job_service.append_event(
        db,
        job_id,
        "voice_generation",
        f"Voice generation finished for {len(dialogue_shots)} dialogue shot(s).",
    )
    if has_dialogue:
        await asyncio.to_thread(finalize_audio_assembly, db, job_id)


def fail_unfinished_shots(
    db: Session,
    job_id: str,
    error: Exception,
    *,
    shot_numbers: set[int] | None = None,
) -> None:
    """Surface a task-level failure instead of leaving real progress pending."""
    job = job_service.get_job(db, job_id)
    result = job_service.job_result(job) if job else None
    if not result:
        return
    error_message = str(error)[:1200]
    result["audio_assembly_pending"] = False
    result["assembly"] = {**result.get("assembly", {}), "provisional": True, "error": error_message}
    job_service.set_result(db, job_id, result)
    for shot in result.get("shots", []):
        shot_number = shot.get("shot_number")
        if shot_numbers is not None and shot_number not in shot_numbers:
            continue
        if shot.get("status") not in {"pending", "generating"}:
            continue
        job_service.update_shot_fields(
            db,
            job_id,
            shot_number,
            status="error",
            error_message=error_message,
            dialogue_audio_url=None,
            dialogue_audio_provider=None,
        )
        job_service.append_shot_status_event(
            db,
            job_id,
            shot_number,
            "error",
            message=f"Voice generation task failed for shot {shot_number}: {error_message}",
            error_message=error_message,
        )
