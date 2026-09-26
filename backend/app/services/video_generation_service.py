"""Module R/S video path with Module T's one bounded compliance rerender."""
import hashlib
import json
import math
import logging
import re
import tempfile
import time
import httpx
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, unquote, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app.config import settings
from app.video_models import AUTOMATIC, validate_selection
from app.services.speech_mode import is_voiceover, is_onscreen_speech
from app.services.dialogue_window import from_shot as dialogue_window_from_shot
from app.services import job_service, storage_service, render_compliance_service, video_references
from app.services.still_frame_service import match_entities, visual_description

MODEL = "seedance-2.0-reference-to-video"
BASE = "https://api.evolink.ai"
CONSTRAINTS = "Constraints: no readable text, logos or extra subjects; preserve identity and physical continuity."
URL = re.compile(r"https?://[^\s<>\"]+")
MODE_WORDS = re.compile(r"\b(?:continu\w*|extend\w*|replac\w*|remov\w*|delet\w*|chang\w*)\b|\bstill entering\b", re.I)


def identity(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def source_fingerprint(shot):
    data = [shot.get(k) for k in ("compiled_prompt", "still_frame_key", "duration_sec", "dialogue_text", "dialogue_audio_key", "has_dialogue", "speech_mode")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def fresh_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Video references require HTTPS URLs")
    bucket = settings.aws_s3_bucket
    if bucket and parsed.netloc == f"{bucket}.s3.{settings.aws_region}.amazonaws.com":
        return storage_service.asset_url(unquote(parsed.path.lstrip('/')), expires_in=86400)
    return url


def translate(result, shot, *, audio_model=None):
    translated = _translate(result, shot, audio_model=audio_model)
    if shot.get("approved_product_references"):
        prompt = translated["request"]["prompt"]
        prompt = prompt.replace("no readable text, logos or extra subjects", "no added captions, invented logos or extra subjects")
        prompt = prompt.replace("No on-screen text, logos or readable signage; composite text in post.", "No added captions or invented logos.")
        translated["request"]["prompt"] = prompt + "\nPreserve the product geometry, materials, colors and existing packaging lettering/logos visible in the accepted scene image. Do not insert any product absent from that scene."
    if translated.get("reference_manifest"):
        video_references.check_prompt(translated["request"]["prompt"], translated["reference_manifest"])
    return translated


def _translate(result, shot, *, audio_model=None):
    if result.get("video_model") == "h3_max_fal":
        if audio_model and audio_model != "h3_max_fal":
            raise ValueError("Shots inherit the video model selected for this job")
        from app.services.h3_video_service import translate as h3_translate
        return h3_translate(result, shot)
    if result.get("video_model") == AUTOMATIC:
        return automatic_translation(result, shot, audio_model=audio_model)
    if is_onscreen_speech(shot):
        from app.services import audio_video_service
        return audio_video_service.translate(result, shot, audio_model)
    from app.services.dialogue_duration import performance_duration
    selected = result.get("video_model")
    if selected == "kling_avatar_fal":
        raise ValueError("This job uses Kling Avatar, which only supports visible speaking shots. Silent and narration-only shots need a scene-video model.")
    if selected == "kling_voice_fal":
        from app.services.audio_video_service import MODELS
        from app.services.still_frame_service import shot_fingerprint
        if not shot.get("compiled_prompt") or not shot.get("still_frame_url") or shot.get("still_frame_status") not in (None, "ready"):
            raise ValueError("Create this shot's accepted preview before generating video")
        if shot.get("still_frame_source_hash") and shot["still_frame_source_hash"] != shot_fingerprint(shot):
            raise ValueError("This preview is out of date; create a new preview first")
        seconds = performance_duration(shot.get("duration_sec") or 0, shot.get("dialogue_audio_duration_sec") or 0)
        if not math.isfinite(seconds) or not 0 < seconds <= 15:
            raise ValueError("Kling scene duration must fit within 15 seconds")
        prompt = visual_description(shot["compiled_prompt"])
        if is_voiceover(shot):
            prompt = re.split(r"Performance reference —|\nDialogue:", prompt, maxsplit=1)[0].strip()
        prompt += "\nNo visible speech. " + ("Silent visuals; narration is added separately." if is_voiceover(shot) else "Ambient sound only, no dialogue or music.")
        if len(prompt) > 2500:
            raise ValueError("Kling scene instructions exceed 2,500 characters")
        return {"provider": "fal", "model": MODELS[selected][1], "mode": "image_to_video",
                "request": {"start_image_url": fresh_url(shot["still_frame_url"]), "prompt": prompt,
                            "duration": str(math.ceil(seconds)), "generate_audio": not is_voiceover(shot)},
                "warnings": ["Kling outputs 4K for this job; the job quality setting does not change this endpoint's output tier."],
                "mode_risk_terms": [], "constraints": CONSTRAINTS}
    if result.get("ai_model") != "Seedance 2.0":
        raise ValueError("Module R supports Seedance 2.0 Reference-to-Video only")
    if not shot.get("compiled_prompt") or not shot.get("still_frame_url"):
        raise ValueError("A compiled prompt and accepted still are required before video generation")
    if shot.get("has_dialogue") and not shot.get("dialogue_audio_url"):
        raise ValueError("Approve and finish dialogue audio before video generation")
    quality = result.get("quality", "720p").lower()
    if quality not in {"480p", "720p", "1080p", "4k"}:
        raise ValueError("Unsupported Seedance quality")
    if is_voiceover(shot) and shot.get("dialogue_audio_duration_sec") is None:
        raise ValueError("Finish narration audio measurement before video generation")
    if float(shot["duration_sec"]) <= 0:
        raise ValueError("A positive planned shot duration is required")
    planned = performance_duration(shot["duration_sec"], shot.get("dialogue_audio_duration_sec") or 0)
    if not math.isfinite(planned) or planned <= 0 or planned > 15:
        raise ValueError("Seedance duration must fit within 15 seconds")
    duration = math.ceil(planned)  # performance_duration enforces the universal shot floor.
    warnings = []
    if duration != planned:
        warnings.append(f"Provider bills {duration}s (whole seconds); planned duration remains {planned:g}s.")
    visual = visual_description(shot["compiled_prompt"])
    if not shot.get("has_dialogue") and shot.get("direction_version") == 1:
        # Directed shots already have an approved, ordered execution contract.
        # Sending the prose Compiler recap as a second action authority can
        # reintroduce an earlier/later-state contradiction at the video model.
        from app.services.dialogue_window import visual_instruction
        visual = visual_instruction(result, shot, "@image1", speaking=False)
    if is_voiceover(shot):
        # The spoken text belongs to post-production, never a visible performance.
        visual = re.split(r"Performance reference —|\nDialogue:", visual, maxsplit=1)[0].strip()
    provider_tag = "fal" if selected and selected.endswith("_fal") else "evolink"
    references = video_references.build(result, shot, limit=9, tag_style=provider_tag, refresh=fresh_url)
    refs = references["images"]
    lookup = references["lookup"]
    warnings.extend(references["warnings"])
    # visual_description removes Module M's fixed URL appendix; the explicit
    # role map above rewrites those references into provider tags. Reject any
    # unknown inline URL rather than leaving a dangling reference.
    def tagged(match):
        raw = match.group().rstrip('.,;)')
        index = lookup.get(identity(raw))
        if index is None:
            raise ValueError("Compiled prompt contains a reference not selected for this shot")
        return index + match.group()[len(raw):]
    visual = URL.sub(tagged, visual)
    visual = re.sub(r"No on-screen text, logos or readable signage; composite text in post\.?", "", visual)
    # Negative-only sentences become one constraints line; physical state facts
    # (e.g. 'no crown yet') embedded in action sentences remain part of the action.
    negatives = []
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", visual.strip()):
        if re.match(r"(?:No\s|Avoid\s|Negative(?: prompt)?\s*:)", sentence, re.I):
            negatives.append(sentence)
        else:
            kept.append(sentence)
    constraints = CONSTRAINTS
    if negatives:
        # Preserve specific prohibitions while removing boilerplate. Never silently
        # discard an unfamiliar physical-state or style constraint to fit a limit.
        short = list(dict.fromkeys(re.sub(r"^(?:Avoid\s|Negative(?: prompt)?\s*:)\s*", "", s, flags=re.I).rstrip('.!') for s in negatives))
        constraints += " " + "; ".join(short) + "."
        if len(constraints.split()) > 55:
            raise ValueError("Negative constraints cannot be safely condensed below 55 words; review required before paid generation")
    audio = ("Audio: silent output; existing Sarvam dialogue will be muxed later."
             if shot.get("has_dialogue") else "Audio: ambient sound only, no music, no dialogue.")
    prompt = references["instructions"] + "\n" + " ".join(kept) + "\n" + constraints + "\n" + audio
    risks = sorted(set(m.group().lower() for m in MODE_WORDS.finditer(prompt)))
    if risks:
        warnings.append("Mode-intent warning: " + ", ".join(risks) + "; submitting explicit reference-to-video model, with no input video. Intent classification is not guaranteed.")
    request = {"model": MODEL, "prompt": prompt, "image_urls": refs,
               "duration": duration, "quality": quality, "aspect_ratio": result.get("aspect_ratio", "16:9"),
               "generate_audio": not bool(shot.get("has_dialogue"))}
    provider_name, model = "evolink", MODEL
    if selected:
        from app.services.audio_video_service import MODELS
        provider_name, model = MODELS[selected]
        supported = {"480p", "720p"} if "_fast_" in selected or "_mini_" in selected else ({"480p", "720p", "1080p"} if provider_name == "fal" else {"480p", "720p", "1080p", "4k"})
        if quality not in supported:
            raise ValueError("The job's selected video model does not support this resolution")
        request["model"] = model
        if provider_name == "fal":
            request.pop("model")
            request["resolution"] = request.pop("quality")
            request["duration"] = str(duration)
            request["prompt"] = re.sub(r"@image(\d+)", r"@Image\1", prompt)
    if is_voiceover(shot) and selected in {"seedance_mini_evolink", "seedance_mini_fal"}:
        from app.services.audio_video_service import approved_speech_text
        tag = "@Audio1" if provider_name == "fal" else "@audio1"
        request["audio_urls"] = [fresh_url(shot["dialogue_audio_url"])]
        request["generate_audio"] = True
        request["prompt"] = request["prompt"].replace(
            "Audio: silent output; existing Sarvam dialogue will be muxed later.",
            f"Audio: use {tag} as off-screen narration. No visible person speaks; do not animate lips. "
            "The approved narration will be preserved exactly during final assembly.")
        request["prompt"] += approved_speech_text(result, shot, shot.get("speaker_label") or "off-screen narrator")
    return {"provider": provider_name, "model": model, "request": request,
            "reference_manifest": references["manifest"], "warnings": warnings, "mode_risk_terms": risks, "constraints": constraints}


def automatic_translation(result, shot, *, audio_model=None):
    """Explicit job policy; use the existing durable submit/poll/QA pipeline."""
    from app.services.still_frame_service import shot_fingerprint
    validate_selection(AUTOMATIC, result.get("ai_model"), result.get("language", "English"), result.get("quality", "720p"))
    if audio_model and audio_model != AUTOMATIC:
        raise ValueError("Shots inherit this job's Automatic routing; per-shot model overrides are not supported")
    if not shot.get("compiled_prompt") or not shot.get("still_frame_url") or shot.get("still_frame_status") not in (None, "ready"):
        raise ValueError("Create this shot's accepted preview before generating video")
    if shot.get("still_frame_source_hash") and shot["still_frame_source_hash"] != shot_fingerprint(shot):
        raise ValueError("This preview is out of date; create a new preview first")
    if result.get("aspect_ratio", "16:9") not in {"16:9", "9:16"}:
        raise ValueError("Automatic supports landscape 16:9 or portrait 9:16")
    translated = _translate({**result, "video_model": "seedance_mini_evolink"}, shot)
    translated["warnings"].append("This saved Automatic job now uses Seedance Mini for every shot.")
    return translated



def provider(method, path, body=None):
    if not settings.evolink_api_key:
        raise ValueError("EVOLINK_API_KEY is not configured")
    req = Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Authorization": "Bearer " + settings.evolink_api_key, "Content-Type": "application/json"}, method=method)
    try:
        with urlopen(req, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        # Do not log prompts, URLs or credentials in shared logs.
        raise RuntimeError(f"EvoLink HTTP {error.code}: inspect provider task/account before retrying") from error


def automatic_review_correction(shot):
    if shot.get("video_status") != "review_required":
        return ""
    expected = shot.get("video_compliance_expected") or {}
    error = shot.get("video_error") or ""
    mismatches = [key for key in ("style", "scale", "staging") if f"{key}:" in error]
    return render_compliance_service.visual_retry_instruction(
        expected, mismatches, visible_characters=shot.get("characters_in_shot"))


def regenerate_translation(result, shot, hint="", *, audio_model=None):
    corrections = [value for value in (automatic_review_correction(shot), hint.strip()) if value]
    hint = "\n".join(corrections)
    translated = translate(result, shot, audio_model=audio_model)
    if is_onscreen_speech(shot) or result.get("video_model") in {AUTOMATIC, "h3_max_fal"}:
        if hint:
            translated["request"]["prompt"] += "\nRequested correction: " + hint.strip()
            translated["warnings"].append("Full regeneration from the accepted preview; existing approved speech is reused where present.")
        return translated
    if hint.strip() and shot.get("video_key"):
        if (result.get("video_model") or "").startswith("kling_"):
            raise ValueError("Video Edit is not available for this job's Kling model. Use Regenerate for a fresh clip.")
        request = translated["request"]
        request["video_urls"] = [storage_service.asset_url(shot["video_key"], expires_in=86400)]
        # Keep the existing reference-array translation, but do not force the old
        # opening still as a new first frame when editing the actual source clip.
        request["prompt"] = ("Edit @video1 (video 1), the existing source clip. Targeted change: " + hint.strip()
            + ". Change only the requested element. Preserve all other subjects, identity, objects, action timing, "
              "duration, composition and sound unless explicitly named in the change. Do not extend the clip. "
              "The @image references are identity/context references only, not replacement opening frames.")
        seconds = int(shot.get("video_requested_duration") or request["duration"])
        request["duration"] = str(seconds) if translated.get("provider") == "fal" else seconds
        if translated.get("provider") == "fal":
            request["prompt"] = request["prompt"].replace("@video1", "@Video1").replace("@image", "@Image")
        translated["warnings"] = ["Targeted video edit requested. Unrelated visual or audio changes remain possible; review the result."]
        translated["mode"] = "video_edit"
    else:
        translated["mode"] = "reference_to_video"
        if hint.strip():
            translated["request"]["prompt"] += "\nRequested correction: " + hint.strip()
        translated["warnings"].append("Full generation: no existing clip with a targeted hint was supplied.")
    return translated


def start(db, job_id, number, *, regenerate=False, hint="", expected_attempt=None, audio_model=None):
    result, shot = job_service.video_source(db, job_id, number)
    if regenerate and not result.get("generation_approved"):
        raise ValueError("Approve the revised plan/audio before video regeneration")
    if regenerate and expected_attempt is None:
        raise ValueError("Refresh the shot before regenerating")
    if audio_model and not is_onscreen_speech(shot):
        raise ValueError("Audio-reference models apply only to visible speaking shots")
    translated = regenerate_translation(result, shot, hint, audio_model=audio_model) if regenerate else translate(result, shot, audio_model=audio_model)
    if translated.get("reference_manifest"):
        video_references.check_prompt(translated["request"]["prompt"], translated["reference_manifest"])
    provider_name = translated.get("provider", "evolink")
    if provider_name == "fal" and not settings.fal_api_key:
        raise ValueError("FAL_API_KEY is not configured")
    if provider_name == "evolink" and not settings.evolink_api_key:
        raise ValueError("EVOLINK_API_KEY is not configured")
    voice_setup = None
    if translated.get("audio_model") == "kling_voice_fal":
        from app.services import kling_voice_service
        voice_setup = kling_voice_service.preparation(db, job_id, result, shot)
    job_service.claim_video(db, job_id, number, {"video_status": "submitting", "video_error": None,
                           "video_provider": provider_name, "video_model": translated.get("model", MODEL),
                           "video_audio_model": translated.get("audio_model"),
                           "video_audio_reference_url": shot.get("dialogue_audio_url") if is_onscreen_speech(shot) else None,
                           "video_onscreen_speech": is_onscreen_speech(shot),
                           "video_dialogue_timing": dialogue_window_from_shot(shot, require=True) if is_onscreen_speech(shot) else None,
                           "video_source_hash": source_fingerprint(shot), "video_submitted_at": datetime.now(timezone.utc).isoformat(),
                           "video_warnings": translated["warnings"], "video_mode": translated.get("mode", "reference_to_video"),
                           "video_compliance_expected": render_compliance_service.snapshot(db, job_id, shot),
                           "video_retry_request": (translate(result, shot)["request"] if translated.get("mode") == "video_edit" else translated["request"]),
                           "video_reference_manifest": translated.get("reference_manifest", []),
                           "video_compliance_retries": 0},
                           replace_token=expected_attempt if regenerate else None)
    for warning in translated["warnings"]:
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: {warning}")
    # Validate/prepare the actual reference before charging for a video. The
    # approved speech stays unchanged; compliance retries reuse this same copy.
    try:
        from app.services import seedance_audio_reference
        audio_evidence = seedance_audio_reference.prepare(translated, shot, job_id)
        if audio_evidence:
            prepared = translated['request'].get('target_audio_url') or (
                translated['request'].get('audio_urls') or
                translated['request'].get('reference_audio_urls') or [None])[0]
            job_service.update_video(db, job_id, number,
                video_audio_reference_validation=audio_evidence,
                video_audio_reference_url=prepared,
                video_retry_request=translated['request'])
            job_service.append_event(db, job_id, 'video_generation',
                f"Shot {number}: audio reference checked; speech {audio_evidence['source_duration_sec']:.6f}s, "
                f"reference {audio_evidence['reference_duration_sec']:.6f}s, "
                f"leading silence {audio_evidence['leading_silence_sec']:.6f}s, "
                f"trailing silence {audio_evidence['trailing_silence_sec']:.6f}s. Approved speech unchanged.")
    except Exception as error:
        db.rollback()
        job_service.update_video(db, job_id, number, video_status='failed', video_error=str(error))
        raise
    try:
        if job_service.video_was_stopped(db, job_id, number):
            return {"status": "stopped"}
        if voice_setup:
            key, sample = voice_setup
            kling_voice_service.ensure(db, key, sample)
            job_service.update_video(db, job_id, number, video_status="processing", video_phase="preparing_voice",
                                     video_task_id="voice:" + key, video_kling_voice_key=key,
                                     video_requested_duration=translated["request"]["duration"], video_generate_audio=True)
            job_service.append_event(db, job_id, "video_generation", f"Shot {number}: preparing reusable Kling voice before scene generation.")
            return {"id": "voice:" + key, "status": "preparing_voice"}
        if provider_name == "fal":
            from app.services import audio_video_service
            response = audio_video_service.submit(translated["model"], translated["request"])
        else:
            response = provider("POST", "/v1/videos/generations", translated["request"])
        task = response.get("id")
        if not isinstance(task, str) or not task:
            raise ValueError("Provider returned no task ID; submission outcome unknown")
    except Exception as error:
        db.rollback()
        job_service.update_video(db, job_id, number, video_status="submission_unknown", video_error=str(error))
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: submission uncertain/failed; no automatic retry to avoid duplicate charges. {error}")
        raise
    # Once the task ID is saved, a trace failure must not demote it to an
    # unknown submission and prevent restart recovery.
    job_service.update_video(db, job_id, number, video_task_id=task, video_status="processing",
                                  video_model=translated.get("model", MODEL), video_usage=response.get("usage"),
                                  video_fal_status_url=response.get("status_url"), video_fal_response_url=response.get("response_url"),
                                  video_requested_duration=translated["request"].get("duration"),
                                  video_generate_audio=translated["request"].get("generate_audio", True))
    job_service.append_event(db, job_id, "video_generation", f"Shot {number}: task {task} submitted; polling for completion.")
    return response


def poll(db, job_id, shot, *, defer_completed=False):
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(shot["video_submitted_at"])).total_seconds()
    if shot["video_status"] == "submitting":
        if age > 180:
            job_service.update_video(db, job_id, shot['shot_number'], expected_submitted_at=shot.get('video_submitted_at'), video_status="submission_unknown", video_error="No task ID was saved after submission. Check provider account; do not resubmit blindly.")
            job_service.append_event(db, job_id, "video_generation", "WARNING: Video submission has no saved task ID after three minutes; stopped for account reconciliation.")
        return
    if age > 23 * 3600:
        job_service.update_video(db, job_id, shot['shot_number'], expected_submitted_at=shot.get('video_submitted_at'), video_status="review_required", video_error="Video task/storage recovery exceeded 23 hours; inspect provider before its result expires.")
        job_service.append_event(db, job_id, "video_generation", "WARNING: Video recovery deadline reached; provider result may expire soon.")
        return
    if shot.get("video_phase") == "preparing_voice":
        from app.services import kling_voice_service
        return kling_voice_service.advance(db, job_id, shot)
    number, task = shot['shot_number'], shot['video_task_id']
    if shot.get('video_provider') == 'hedra':
        from app.services import hedra_video_service
        return hedra_video_service.poll(db, job_id, shot)
    if shot.get("video_provider") == "fal":
        from app.services import audio_video_service
        try:
            response = audio_video_service.poll(shot)
        except audio_video_service.FalResultError as error:
            response = {"status": "failed", "error": str(error)}
    else:
        response = provider("GET", "/v1/tasks/" + quote(task, safe=""))
    if response.get("model") and response["model"] != (shot.get("video_model") or MODEL):
        job_service.update_video(db, job_id, number, expected_task_id=task, video_status="review_required", video_error="Provider reported a different model/mode; review before proceeding")
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: provider model mismatch; stopped for review.")
        return
    if response.get("status") == "failed":
        job_service.update_video(db, job_id, number, expected_task_id=task, video_status="failed", video_error=json.dumps(response.get("error")))
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: provider task failed; no paid regeneration attempted.")
    elif response.get("status") == "completed":
        if defer_completed:
            return response
        finish_completed(db, job_id, shot, response)


class CompletionCache:
    """Task-scoped temporary disk storage, bounded by the dispatcher's media slots.

    No user media is retained after completion/lease loss. A process restart may
    redownload; durable compliance verdicts still survive in the task record.
    """
    def __init__(self, task):
        self.task = task
        self.media = tempfile.TemporaryFile()
        self.downloaded = False
        self.validated = False
        self.checks = {}
        self.stored = None
        self.size = 0
        self.digest = None
        self.attempts = 0
        self.processing_sec = 0

    def close(self):
        self.media.close()


def finish_completed(db, job_id, shot, response, *, completion=None):
    cache = completion or CompletionCache(shot['video_task_id'])
    started = time.monotonic()
    try:
        if cache.task != shot['video_task_id']:
            raise ValueError('Completion cache belongs to another provider task')
        return _finish_completed(db, job_id, shot, response, cache)
    finally:
        cache.attempts += 1
        cache.processing_sec += time.monotonic() - started
        if completion is None:
            cache.close()


def _finish_completed(db, job_id, shot, response, cache):
    number, task = shot['shot_number'], shot['video_task_id']
    started = time.monotonic()
    timings = {}
    urls = response.get("results") or []
    if not urls:
        raise ValueError("Completed video task has no downloadable result")
    if urlsplit(urls[0]).scheme != "https":
        raise ValueError("Provider download must use HTTPS")
    with nullcontext(cache.media) as video:
        phase = time.monotonic()
        # The provider CDN rejects urllib with Cloudflare 1010; the app's
        # existing HTTPX client is accepted without credentials or a proxy.
        timings['download_reused'] = cache.downloaded
        if not cache.downloaded:
            video.seek(0); video.truncate()
            digest, size = hashlib.sha256(), 0
            with httpx.stream("GET", urls[0], timeout=120, follow_redirects=True) as download:
                download.raise_for_status()
                for chunk in download.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError("Video exceeds 512MB download bound")
                    digest.update(chunk); video.write(chunk)
            cache.size, cache.digest, cache.downloaded = size, digest.hexdigest(), True
        size = cache.size
        timings["download_sec"] = time.monotonic() - phase
        phase = time.monotonic()
        video.seek(0)
        if video.read(12)[4:8] != b"ftyp":
            raise ValueError("Downloaded result is not an MP4 container")
        video.seek(0)
        if shot.get("video_audio_model") and not cache.validated:
            from app.services import audio_video_service
            try:
                audio_video_service.validate_audio_result(video)
            except ValueError as error:
                job_service.update_video(db, job_id, number, expected_task_id=task,
                    video_status="review_required", video_error=str(error))
                job_service.append_event(db, job_id, "video_generation", f"Shot {number}: {error}")
                return
        cache.validated = True
        timings["media_validation_sec"] = time.monotonic() - phase
        phase = time.monotonic()
        timings['compliance_check_reused'] = task in cache.checks
        accepted = render_compliance_service.accept(db, job_id, shot, video, check_cache=cache.checks)
        timings["compliance_sec"] = time.monotonic() - phase
        if not accepted:
            job_service.append_event(db, job_id, "video_timing", f"Shot {number}: completion deferred by compliance; " + json.dumps(timings))
            return
        # Compliance may remove unwanted speech from a silent shot while
        # preserving its picture. Hash the bytes actually sent to storage.
        if cache.checks.get("_silent_cleaned"):
            video.seek(0)
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: video.read(1024 * 1024), b""):
                digest.update(chunk); size += len(chunk)
            cache.digest, cache.size = digest.hexdigest(), size
        phase = time.monotonic()
        video.seek(0)
        key = f"jobs/{job_id}/videos/{number}-{task}.mp4"
        if cache.stored is None:
            cache.stored = storage_service.upload_file(key, video, content_type="video/mp4")
        stored = cache.stored
    timings["upload_sec"] = time.monotonic() - phase
    timings["completion_processing_sec"] = time.monotonic() - started
    timings['completion_attempts'] = cache.attempts + 1
    timings['total_completion_processing_sec'] = cache.processing_sec + timings['completion_processing_sec']
    job_service.update_video(db, job_id, number, expected_task_id=task, video_processing_timings=timings, video_status="done", video_url=stored['url'],
                                  video_key=key, video_sha256=cache.digest, video_bytes=size,
                                  video_usage=response.get("usage"), video_error=None,
                                  video_stored_at=datetime.now(timezone.utc).isoformat())
    job_service.append_event(db, job_id, "video_generation", f"Shot {number}: video persisted to S3 ({size} bytes, SHA256 {cache.digest}); provider expiry no longer controls retention.")


def polling_loop(stop):
    from app.services.video_task_worker import run
    run(stop)
