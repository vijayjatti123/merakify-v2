"""Reviewed per-shot candidates; accepted media stays untouched until explicit use."""
import copy
import io
import time
import uuid
import warnings

from PIL import Image, ImageOps

from app.db import SessionLocal
from app.services import job_service, still_frame_service as still, storage_service

MAX_BYTES = 15 * 1024 * 1024


def expired(candidate):
    return time.time() - candidate.get("started_at", 0) > 600


def _hash(result, shot):
    from app.services.preview_plan import preview_input
    current = copy.deepcopy(shot)
    if current.get("preview_input"):
        current["preview_input"] = preview_input(result, current)
    return still.shot_fingerprint(current)


def _available(db, job, result, shot):
    from app.models import VideoTask, FinalAssembly
    import json
    if job.status != "done" or not result.get("generation_approved"):
        raise ValueError("Approve your written plan before changing its images.")
    if result.get("preview_preparation_pending") or result.get("audio_assembly_pending") or shot.get("still_frame_status") == "generating":
        raise ValueError("Wait for preview preparation to finish first.")
    task = db.query(VideoTask).filter_by(job_id=job.id, shot_number=shot["shot_number"]).populate_existing().one_or_none()
    if task and json.loads(task.data_json).get("video_status") in {"submitting", "processing", "submission_unknown"}:
        raise ValueError("Wait for this video's generation to finish before replacing its image.")
    assembly = db.query(FinalAssembly).filter_by(job_id=job.id).populate_existing().one_or_none()
    if assembly and json.loads(assembly.data_json).get("status") == "running":
        raise ValueError("Wait for final assembly to finish before replacing an image.")


def claim(db, job_id, number, expected_key, kind):
    token = uuid.uuid4().hex
    def change(job, result, shot):
        _available(db, job, result, shot)
        if (shot.get("still_frame_key") or "") != expected_key:
            raise ValueError("This image changed. Refresh before replacing it.")
        old = shot.get("preview_replacement", {})
        if old.get("status") == "ready" or (old.get("status") == "working" and not expired(old)):
            raise ValueError("Review or discard the current replacement first.")
        if not (shot.get("preview_input") or shot.get("compiled_prompt")):
            raise ValueError("Finish preparing this shot before changing its image.")
        candidate = {"token": token, "status": "working", "kind": kind, "started_at": time.time(),
                     "source_hash": _hash(result, shot), "previous_key": expected_key}
        shot["preview_replacement"] = candidate
        snapshot = copy.deepcopy(result)
        snapshot["aspect_ratio"] = job.aspect_ratio
        return snapshot
    return token, job_service.mutate_preview_replacement(db, job_id, number, change)


def finish(db, job_id, number, token, fields):
    def change(job, result, shot):
        candidate = shot.get("preview_replacement", {})
        if candidate.get("token") != token or candidate.get("status") != "working" or expired(candidate):
            return False
        if candidate["source_hash"] != _hash(result, shot) or candidate["previous_key"] != (shot.get("still_frame_key") or ""):
            candidate.update(status="failed", warning="The shot changed while this image was being prepared. Try again with the updated plan.")
            return False
        candidate.update(fields)
        return True
    return job_service.mutate_preview_replacement(db, job_id, number, change)


def decide(db, job_id, number, token, accept, acknowledge=False):
    def change(job, result, shot):
        candidate = shot.get("preview_replacement", {})
        if candidate.get("token") != token:
            raise ValueError("This replacement changed. Refresh before continuing.")
        if not accept:
            shot.pop("preview_replacement", None)
            return
        _available(db, job, result, shot)
        if candidate.get("status") != "ready" or not candidate.get("key"):
            raise ValueError("Wait for the replacement image to be ready.")
        if candidate["source_hash"] != _hash(result, shot) or candidate["previous_key"] != (shot.get("still_frame_key") or ""):
            raise ValueError("The shot changed. Discard this replacement and try again.")
        if candidate.get("warning") and not acknowledge:
            raise ValueError("Review and acknowledge the image warning before using it.")
        # Previous paid output remains accessible; its source hash now marks it stale.
        shot["previous_still_frame_key"] = shot.get("still_frame_key")
        shot.update(still_frame_key=candidate["key"], still_frame_url=candidate["url"],
                    still_frame_source_hash=_hash(result, shot), still_frame_status="ready")
        shot.pop("still_frame_warning", None)
        shot.pop("preview_replacement", None)
        # Do not feed an obsolete accepted image to future shots. Existing siblings
        # keep their accepted pixels; new references come only from checked candidates.
        refs = result.setdefault("entity_references", {})
        for key in list(refs):
            if refs[key].get("shot_number") == number:
                refs.pop(key)
        for key, ref in candidate.get("entity_references", {}).items():
            if ref.get("shot_number") == number:
                refs[key] = {**ref, "source_hash": shot["still_frame_source_hash"]}
    job_service.mutate_preview_replacement(db, job_id, number, change)
    job_service.append_event(db, job_id, "still_frame", f"Shot {number}: replacement image {'accepted; existing video must be regenerated' if accept else 'discarded; current image preserved'}.")


def normalize_upload(data, aspect_ratio, fit):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Choose a PNG, JPEG or WebP image under 15 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "is_animated", False):
                    raise ValueError("Choose a single PNG, JPEG or WebP image.")
                if source.width * source.height > 25_000_000 or min(source.size) < 256:
                    raise ValueError("Use an image at least 256 pixels on each side and no larger than 25 megapixels.")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError) as error:
        raise ValueError("This image could not be read safely. Choose a different image.") from error
    background = Image.new("RGBA", image.size, "#f4f1ed")
    background.alpha_composite(image)
    image = background.convert("RGB")
    a, b = map(int, aspect_ratio.split(":"))
    edge = min(3840, max(1280, max(image.size)))
    size = (round(edge * a / max(a, b)), round(edge * b / max(a, b)))
    if fit == "crop":
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    elif fit == "fit":
        image = ImageOps.pad(image, size, method=Image.Resampling.LANCZOS, color="#f4f1ed")
    else:
        raise ValueError("Choose Crop to fill or Fit whole image.")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def run(job_id, number, token, snapshot, hint="", upload=None):
    """Reuse Module O's actual two-candidate generator/checker on a private copy."""
    with SessionLocal() as db:
        emit = lambda key, note: job_service.append_event(db, job_id, key, note)
        try:
            shot = next(s for s in snapshot["shots"] if s["shot_number"] == number)
            if upload is None:
                for key in ("still_frame_url", "still_frame_key", "still_frame_source_hash"):
                    shot.pop(key, None)
                snapshot["entity_references"] = {k: v for k, v in snapshot.get("entity_references", {}).items() if v.get("shot_number") != number}
                still.generate_still_frames(snapshot, job_id=job_id, emit=emit, shot_numbers={number}, feedback_by_shot={number: hint})
                shot = next(s for s in snapshot["shots"] if s["shot_number"] == number)
                if not shot.get("still_frame_key"):
                    raise still.StillFrameError("Both candidates failed or the image service was unavailable.")
                fields = {"status": "ready", "key": shot["still_frame_key"], "url": shot["still_frame_url"],
                          "entity_references": snapshot.get("entity_references", {})}
            else:
                # Uploaded pixels are a deliberate user choice, never silently regenerated.
                # Check against locked identity/style and display any discrepancy for review.
                from app.services.character_image_service import GeneratedCharacterImage, _download_reference_image
                from app.services.preview_plan import preview_visual, visual_contract
                facts = shot.get("preview_input")
                visual = preview_visual(facts) if facts else still.visual_description(shot["compiled_prompt"])
                request_contract = shot.get("still_frame_contract") or (visual_contract(facts, visual) if facts else visual)
                warning = ""
                try:
                    refs = []
                    names = {n.casefold() for n in shot.get("characters_in_shot", [])}
                    for character in snapshot.get("continuity", {}).get("characters", []):
                        if character.get("name", "").casefold() in names and character.get("image_url"):
                            url = still.fresh_reference(character["image_url"])
                            refs.append((character["name"], _download_reference_image(url), url, 0, 0))
                    verdict = still.check_still(request_contract, refs, GeneratedCharacterImage(data=upload, content_type="image/png"), emit=emit)
                    if not verdict["approved"]:
                        warning = "This image differs from the shot plan: " + verdict["reason"]
                except Exception:
                    warning = "We couldn't check this image against the planned scene and character. Review it carefully before using it."
                stored = storage_service.upload_bytes(key=f"jobs/{job_id}/stills/{number}-upload-{token}.png", body=upload, content_type="image/png")
                fields = {"status": "ready", "key": stored["key"], "url": stored["url"], "warning": warning}
            finish(db, job_id, number, token, fields)
        except Exception as error:
            db.rollback()
            finish(db, job_id, number, token, {"status": "failed", "warning": "Couldn't prepare a replacement image. Your current image is unchanged. Try again."})
            emit("still_frame", f"WARNING: Shot {number}: image replacement failed ({type(error).__name__}); current image preserved.")
