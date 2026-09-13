"""Module R: one explicit Seedance 2.0 reference-to-video path, no auto rerenders."""
import hashlib
import json
import math
import logging
import re
import tempfile
import httpx
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, unquote, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app.config import settings
from app.services import job_service, storage_service
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
    data = [shot.get(k) for k in ("compiled_prompt", "still_frame_key", "duration_sec", "dialogue_text", "dialogue_audio_key", "has_dialogue")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def fresh_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Video references require HTTPS URLs")
    bucket = settings.aws_s3_bucket
    if bucket and parsed.netloc == f"{bucket}.s3.{settings.aws_region}.amazonaws.com":
        return storage_service.asset_url(unquote(parsed.path.lstrip('/')), expires_in=86400)
    return url


def translate(result, shot):
    if shot.get("has_dialogue"):
        from app.services import hedra_video_service
        return hedra_video_service.preview(result, shot)
    if result.get("ai_model") != "Seedance 2.0":
        raise ValueError("Module R supports Seedance 2.0 Reference-to-Video only")
    if not shot.get("compiled_prompt") or not shot.get("still_frame_url"):
        raise ValueError("A compiled prompt and accepted still are required before video generation")
    if shot.get("has_dialogue") and not shot.get("dialogue_audio_url"):
        raise ValueError("Approve and finish dialogue audio before video generation")
    quality = result.get("quality", "720p").lower()
    if quality not in {"480p", "720p", "1080p", "4k"}:
        raise ValueError("Unsupported Seedance quality")
    planned = float(shot["duration_sec"])
    if not math.isfinite(planned) or planned <= 0 or planned > 15:
        raise ValueError("Seedance duration must fit within 15 seconds")
    duration = max(4, math.ceil(planned))
    warnings = []
    if duration != planned:
        warnings.append(f"Provider bills {duration}s (integer minimum 4s); planned duration remains {planned:g}s.")
    visual = visual_description(shot["compiled_prompt"])
    candidates = [("shot opening/state", shot["still_frame_url"])]
    names = {n.strip().casefold() for n in shot.get("characters_in_shot", [])}
    for char in result.get("continuity", {}).get("characters", []):
        if char.get("character_id") and char["name"].strip().casefold() in names and char.get("image_url"):
            candidates.append(("character " + char["name"], char["image_url"]))
    needed = match_entities(result, shot, visual)
    for name, ref in result.get("entity_references", {}).items():
        if name in needed and ref.get("url"):
            candidates.append(("job entity " + name, ref["url"]))
    refs, lookup, roles = [], {}, []
    for role, url in candidates:
        key = identity(url)
        if key in lookup:
            roles.append(f"{role}: @image{lookup[key]}")
            continue
        if len(refs) == 9:
            warnings.append(f"Reference limit: dropped {role}.")
            continue
        refs.append(fresh_url(url)); lookup[key] = len(refs)
        roles.append(f"{role}: @image{len(refs)}")
    # visual_description removes Module M's fixed URL appendix; the explicit
    # role map above rewrites those references into provider tags. Reject any
    # unknown inline URL rather than leaving a dangling reference.
    def tagged(match):
        raw = match.group().rstrip('.,;)')
        index = lookup.get(identity(raw))
        if index is None:
            raise ValueError("Compiled prompt contains a reference not selected for this shot")
        return f"@image{index}" + match.group()[len(raw):]
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
    prompt = "Reference roles: " + "; ".join(dict.fromkeys(roles)) + ".\n" + " ".join(kept) + "\n" + constraints + "\n" + audio
    risks = sorted(set(m.group().lower() for m in MODE_WORDS.finditer(prompt)))
    if risks:
        warnings.append("Mode-intent warning: " + ", ".join(risks) + "; submitting explicit reference-to-video model, with no input video. Intent classification is not guaranteed.")
    return {"request": {"model": MODEL, "prompt": prompt, "image_urls": refs,
                        "duration": duration, "quality": quality, "aspect_ratio": result.get("aspect_ratio", "16:9"),
                        "generate_audio": not bool(shot.get("has_dialogue"))},
            "warnings": warnings, "mode_risk_terms": risks, "constraints": constraints}


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


def regenerate_translation(result, shot, hint=""):
    translated = translate(result, shot)
    if shot.get("has_dialogue"):
        if hint:
            translated["warnings"].append("Hedra regenerates the full performance; targeted video editing is unavailable.")
        return translated
    if hint.strip() and shot.get("video_key"):
        request = translated["request"]
        request["video_urls"] = [storage_service.asset_url(shot["video_key"], expires_in=86400)]
        # Keep the existing reference-array translation, but do not force the old
        # opening still as a new first frame when editing the actual source clip.
        request["prompt"] = ("Edit @video1 (video 1), the existing source clip. Targeted change: " + hint.strip()
            + ". Change only the requested element. Preserve all other subjects, identity, objects, action timing, "
              "duration, composition and sound unless explicitly named in the change. Do not extend the clip. "
              "The @image references are identity/context references only, not replacement opening frames.")
        request["duration"] = int(shot.get("video_requested_duration") or request["duration"])
        translated["warnings"] = ["Targeted video edit requested. Unrelated visual or audio changes remain possible; review the result."]
        translated["mode"] = "video_edit"
    else:
        translated["mode"] = "reference_to_video"
        if hint.strip():
            translated["request"]["prompt"] += "\nRequested correction: " + hint.strip()
        translated["warnings"].append("Full generation: no existing clip with a targeted hint was supplied.")
    return translated


def start(db, job_id, number, *, regenerate=False, hint="", expected_attempt=None):
    result, shot = job_service.video_source(db, job_id, number)
    if regenerate and not result.get("generation_approved"):
        raise ValueError("Approve the revised plan/audio before video regeneration")
    if regenerate and expected_attempt is None:
        raise ValueError("Refresh the shot before regenerating")
    if shot.get("has_dialogue"):
        from app.services import hedra_video_service
        return hedra_video_service.start(db, job_id, number, result, shot,
            replace_token=expected_attempt if regenerate else None, hint=hint)
    translated = regenerate_translation(result, shot, hint) if regenerate else translate(result, shot)
    job_service.claim_video(db, job_id, number, {"video_status": "submitting", "video_error": None,
                           "video_source_hash": source_fingerprint(shot), "video_submitted_at": datetime.now(timezone.utc).isoformat(),
                           "video_warnings": translated["warnings"], "video_mode": translated.get("mode", "reference_to_video")},
                           replace_token=expected_attempt if regenerate else None)
    for warning in translated["warnings"]:
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: {warning}")
    try:
        response = provider("POST", "/v1/videos/generations", translated["request"])
        task = response.get("id")
        if not isinstance(task, str) or not task:
            raise ValueError("EvoLink returned no task ID; submission outcome unknown")
    except Exception as error:
        db.rollback()
        job_service.update_video(db, job_id, number, video_status="submission_unknown", video_error=str(error))
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: submission uncertain/failed; no automatic retry to avoid duplicate charges. {error}")
        raise
    # Once the task ID is saved, a trace failure must not demote it to an
    # unknown submission and prevent restart recovery.
    job_service.update_video(db, job_id, number, video_task_id=task, video_status="processing",
                                  video_model=response.get("model"), video_usage=response.get("usage"),
                                  video_requested_duration=translated["request"]["duration"],
                                  video_generate_audio=translated["request"]["generate_audio"])
    job_service.append_event(db, job_id, "video_generation", f"Shot {number}: task {task} submitted; polling for completion.")
    return response


def poll(db, job_id, shot):
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
    number, task = shot['shot_number'], shot['video_task_id']
    if shot.get('video_provider') == 'hedra':
        from app.services import hedra_video_service
        return hedra_video_service.poll(db, job_id, shot)
    response = provider("GET", "/v1/tasks/" + quote(task, safe=""))
    if response.get("model") and response["model"] != MODEL:
        job_service.update_video(db, job_id, number, expected_task_id=task, video_status="review_required", video_error="Provider reported a different model/mode; review before proceeding")
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: provider model mismatch; stopped for review.")
        return
    if response.get("status") == "failed":
        job_service.update_video(db, job_id, number, expected_task_id=task, video_status="failed", video_error=json.dumps(response.get("error")))
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: provider task failed; no paid regeneration attempted.")
    elif response.get("status") == "completed":
        urls = response.get("results") or []
        if not urls:
            raise ValueError("Completed video task has no downloadable result")
        if urlsplit(urls[0]).scheme != "https":
            raise ValueError("Provider download must use HTTPS")
        with tempfile.TemporaryFile() as video:
            digest, size = hashlib.sha256(), 0
            # The provider CDN rejects urllib with Cloudflare 1010; the app's
            # existing HTTPX client is accepted without credentials or a proxy.
            with httpx.stream("GET", urls[0], timeout=120, follow_redirects=True) as download:
                download.raise_for_status()
                for chunk in download.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError("Video exceeds 512MB download bound")
                    digest.update(chunk); video.write(chunk)
            video.seek(0)
            if video.read(12)[4:8] != b"ftyp":
                raise ValueError("Downloaded result is not an MP4 container")
            video.seek(0)
            key = f"jobs/{job_id}/videos/{number}-{task}.mp4"
            stored = storage_service.upload_file(key, video, content_type="video/mp4")
        job_service.update_video(db, job_id, number, expected_task_id=task, video_status="done", video_url=stored['url'],
                                      video_key=key, video_sha256=digest.hexdigest(), video_bytes=size,
                                      video_usage=response.get("usage"), video_error=None,
                                      video_stored_at=datetime.now(timezone.utc).isoformat())
        job_service.append_event(db, job_id, "video_generation", f"Shot {number}: video persisted to S3 ({size} bytes, SHA256 {digest.hexdigest()}); provider expiry no longer controls retention.")


def polling_loop(stop):
    """Persisted task IDs survive process restarts. Never resubmit a POST here."""
    from app.db import SessionLocal
    while not stop.wait(10):
        with SessionLocal() as db:
            try:
                pending = job_service.pending_videos(db)
            except Exception:
                logging.getLogger(__name__).error("Video task scan failed; will retry on next tick")
                continue
            for job_id, shot in pending:
                try:
                    poll(db, job_id, shot)
                except Exception as error:
                    db.rollback()
                    message = f"Video polling/storage temporarily failed: {type(error).__name__}; saved task will be polled again."
                    if shot.get("video_error") != message:
                        try:
                            job_service.update_video(db, job_id, shot['shot_number'], expected_submitted_at=shot.get('video_submitted_at'), video_error=message)
                            job_service.append_event(db, job_id, "video_generation", f"Shot {shot['shot_number']}: {message}")
                        except Exception:
                            db.rollback()
                            logging.getLogger(__name__).error("Video recovery warning could not be persisted; next tick will retry")
