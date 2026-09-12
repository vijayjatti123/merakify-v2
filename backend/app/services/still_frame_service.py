"""Optional still previews after compilation; direct Google images, never video."""
import base64
import hashlib
import io
import json
import re
import unicodedata
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps

from app.config import settings
from app.services import storage_service
from app.services.character_image_service import GeneratedCharacterImage, _download_reference_image


class StillFrameError(RuntimeError):
    pass


# Allow provider resolution rounding (e.g. 1376x768 for 16:9), not wrong orientation.
ASPECT_RATIO_TOLERANCE = 0.02


def check_dimensions(image, aspect_ratio):
    """Decode pixels locally; dimension compliance says nothing about camera angle/cropping."""
    try:
        with Image.open(io.BytesIO(image.data)) as decoded:
            decoded.load()  # Reject corrupt/truncated image payloads, not just bad headers.
            width, height = ImageOps.exif_transpose(decoded).size
        expected_width, expected_height = map(int, aspect_ratio.split(":"))
        expected = expected_width / expected_height
        if expected <= 0:
            raise ValueError("Invalid aspect ratio")
    except Exception as error:
        raise StillFrameError("Still image could not be decoded or target aspect ratio is invalid") from error
    deviation = abs((width / height) / expected - 1)
    return {"width": width, "height": height, "expected_aspect_ratio": aspect_ratio,
            "relative_error": deviation, "matches": deviation <= ASPECT_RATIO_TOLERANCE}


def shot_fingerprint(shot):
    """Hide previews after edits rather than show an image of an outdated plan."""
    fields = ("compiled_prompt", "description", "camera_angle", "camera_movement",
              "lighting", "composition_note", "characters_in_shot", "dialogue_text")
    return hashlib.sha256(json.dumps({k: shot.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def invalidate_changed_stills(result):
    for shot in result.get("shots", []):
        if shot.get("still_frame_source_hash") and shot["still_frame_source_hash"] != shot_fingerprint(shot):
            shot["still_frame_url"] = None
            shot.pop("still_frame_key", None)
            shot["still_frame_warning"] = "Still preview is out of date after shot edits; awaiting final shot planning."
    shots = {s.get("shot_number"): s for s in result.get("shots", [])}
    for entity, reference in list(result.get("entity_references", {}).items()):
        source = shots.get(reference.get("shot_number"), {})
        if not source.get("still_frame_key") or reference.get("source_hash") != shot_fingerprint(source):
            result["entity_references"].pop(entity)


def fresh_reference(url):
    """Renew our own object URLs without changing the locked reference object."""
    parsed = urlsplit(url)
    hosts = {f"{settings.aws_s3_bucket}.s3.{settings.aws_region}.amazonaws.com",
             f"{settings.aws_s3_bucket}.s3.amazonaws.com"}
    if settings.aws_s3_bucket and parsed.scheme == "https" and parsed.hostname in hosts:
        return storage_service.asset_url(unquote(parsed.path.lstrip("/")))
    return url


def visual_description(compiled):
    # Module M appends these fixed sections AFTER visual prose. Consume its
    # output without sending spoken quotes, audio instructions or signed URLs.
    for boundary in ("Maintain visual consistency with these reference images — ",
                     "Performance reference — ", "Visual performance only;",
                     "No on-screen text, logos or readable signage;", "\nDialogue:"):
        compiled = compiled.split(boundary, 1)[0]
    if not compiled.strip():
        raise StillFrameError("Compiled visual description is empty")
    return compiled.strip()


def _inline(image):
    return {"inlineData": {"mimeType": image.content_type,
                           "data": base64.b64encode(image.data).decode("ascii")}}


def _google(parts, *, aspect_ratio=None):
    if not settings.google_ai_api_key.strip():
        raise StillFrameError("Google image credentials are not configured")
    config = {"responseModalities": ["IMAGE" if aspect_ratio else "TEXT"]}
    if aspect_ratio:
        config["imageConfig"] = {"aspectRatio": aspect_ratio}
    else:
        config["responseMimeType"] = "application/json"
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(settings.gemini_image_model.strip(), safe='')}:generateContent",
        data=json.dumps({"contents": [{"parts": parts}], "generationConfig": config}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": settings.google_ai_api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except HTTPError as error:
        # Never put provider payloads, source prompts or signed URLs in shared traces.
        raise StillFrameError(f"Google still-frame request failed (HTTP {error.code})") from error


def generate_still(visual, references, aspect_ratio, feedback=""):
    parts = []
    for name, image in references:
        parts.extend([{"text": f"Locked identity reference for {name}:"}, _inline(image)])
    parts.append({"text": (
        "Generate ONE still image: the opening frame of this compiled film shot. "
        "Freeze the first described physical instant; later motion and transition descriptions are "
        "context only, not additional panels or a later completed action. Preserve the specified "
        "framing, subject placement, lighting, palette and rendering style. Supplied reference images "
        "lock each named character's identity, face, hair and clothing; do not replace their face. "
        "Reference sheets are identity guides, never reproduce their layout. No collage, captions, "
        "logos, readable text or audio.\nCompiled visual description:\n" + visual
        + ("\nCorrect the previous visual check: " + feedback if feedback else "")
    )})
    response = _google(parts, aspect_ratio=aspect_ratio)
    for candidate in response.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            data = part.get("inlineData", {})
            if data.get("data") and not part.get("thought"):
                raw = base64.b64decode(data["data"], validate=True)
                mime = data.get("mimeType", "")
                if raw and mime in {"image/png", "image/jpeg", "image/webp"}:
                    return GeneratedCharacterImage(raw, mime)
    raise StillFrameError("Google returned no usable still image")


def check_still(visual, references, image, *, emit=None, entities=None):
    parts = [{"text": (
        "Check this single opening-frame preview against the compiled visual description and any "
        "locked character references. Reject clear identity/outfit changes, wrong subject or framing, "
        "collages, readable text, or a later completed action instead of the described opening. "
        "Ignore motion/audio requirements that cannot be depicted in a still. Do not demand new "
        "details absent from the description. Return JSON only: {\"approved\": boolean, \"reason\": string}.\n"
        + visual
    )}]
    if entities:
        parts.append({"text": "Also return visible_entities: an array of the exact entity IDs below that are "
                      "visibly identifiable in the candidate. Omit offscreen, merely mentioned, or too-cropped "
                      "entities. For job references compare ONLY the named entity's identity/design/material; "
                      "allow the new framing, pose, action and lighting. Reject changed faces, garments, prop "
                      "designs or location surfaces. Entity catalog: " + json.dumps(entities)})
    for name, reference in references:
        parts.extend([{"text": f"Identity reference: {name}"}, _inline(reference)])
    parts.extend([{"text": "Candidate opening frame to check:"}, _inline(image)])
    for attempt in range(2):
        response = _google(parts)
        output = [p for c in response.get("candidates", [])
                  for p in c.get("content", {}).get("parts", []) if not p.get("thought")]
        content = "".join(p.get("text", "") for p in output)
        if content.strip() or not any(p.get("inlineData", {}).get("mimeType", "").startswith("image/") for p in output):
            break
        if emit:
            emit("still_frame", "WARNING: Still QA returned an image instead of JSON text. "
                 + ("Retrying the identical QA request once." if attempt == 0 else "Retry exhausted; using text fallback."))
        if attempt == 1:
            raise StillFrameError("Still QA returned image instead of JSON text twice")
    verdict = json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
    # Google can return a null reason for approval. No correction text is needed
    # then; a rejection still requires a real explanation for the retry.
    if verdict.get("approved") is True and verdict.get("reason") is None:
        verdict["reason"] = "Approved"
    if not isinstance(verdict.get("approved"), bool) or not isinstance(verdict.get("reason"), str):
        raise StillFrameError("Invalid still-frame visual check")
    return verdict


def _words(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def _entity_anchor(name):
    # A bounded English noun-phrase heuristic, not a general noun parser:
    # 'Strainer with tea leaves' anchors on strainer, never on its contents.
    # Shared colors/materials and qualifier words cannot trigger loose matches.
    # Synonyms, unusual word order and other languages may still be missed.
    words = _words(name)
    for index, word in enumerate(words):
        if word in {"with", "without", "of", "for", "in", "on", "at"}:
            words = words[:index]
            break
    return words[-1] if words else None


def match_entities(result, shot, visual):
    """Conservative lexical matching, not semantic coreference. Synonyms can be missed.

    Unique main-object anchors cover 'Plain Cup' -> 'plain ceramic cup' and
    'Strainer with tea leaves' -> 'metal strainer'. Ambiguous anchors are never
    guessed. QA must establish actual visibility before seeding.
    """
    catalog = {}
    for kind in ("characters", "props", "locations"):
        for entity in result.get("continuity", {}).get(kind, []):
            if not isinstance(entity, dict) or not entity.get("name") or entity.get("character_id"):
                continue
            catalog[kind + ":" + " ".join(_words(entity["name"]))] = (kind, entity)
    text = _words(visual)
    visible = {" ".join(_words(n)) for n in shot.get("characters_in_shot", [])}
    matches = {}
    for key, (kind, entity) in catalog.items():
        words = _words(entity["name"])
        if not words:
            continue
        exact = " " + " ".join(words) + " " in " " + " ".join(text) + " "
        head = _entity_anchor(entity["name"])
        unique = sum(_entity_anchor(e["name"]) == head for k, e in catalog.values() if k == kind) == 1
        noun = head is not None and (head in text or (head == "counter" and "countertop" in text))
        if (kind == "characters" and " ".join(words) in visible) or (kind != "characters" and (exact or unique and noun)):
            matches[key] = entity["name"]
    return matches


def generate_still_frames(result, *, job_id, emit):
    """A failed shot remains usable as text; never convert image failures to job errors."""
    characters = {c["name"].strip().casefold(): c for c in result.get("continuity", {}).get("characters", [])}
    # References live only in this job's result JSON, pointing to its own stills.
    # Rebuild on each full still pass so revised plans cannot inherit stale anchors.
    reference_map = result["entity_references"] = {}
    reference_bytes = {}
    for shot in sorted(result.get("shots", []), key=lambda s: s["shot_number"]):
        shot["still_frame_url"] = None
        shot.pop("still_frame_key", None)
        shot.pop("still_frame_source_hash", None)
        shot.pop("still_frame_warning", None)
        if not shot.get("compiled_prompt"):
            continue  # Dialogue jobs wait for real post-approval compilation.
        number = shot["shot_number"]
        try:
            visual = visual_description(shot["compiled_prompt"])
            entities = match_entities(result, shot, visual)
            references = []
            for name in shot.get("characters_in_shot", []):
                character = characters.get(name.strip().casefold(), {})
                if character.get("character_id"):
                    if not character.get("image_url"):
                        raise StillFrameError("Locked character has no reference image")
                    references.append((name, _download_reference_image(fresh_reference(character["image_url"]))))
            for entity_id in entities:
                if entity_id in reference_map:
                    references.append(("Job entity " + entity_id + "; preserve only this entity, not the old shot layout",
                                       reference_bytes[entity_id]))
                    emit("still_frame", f"Shot {number}: conditioning {entity_id} from job reference shot {reference_map[entity_id]['shot_number']}.")
            if entities:
                visual += ("\nJob-scoped consistency: reference frames lock ONLY each labeled entity's face, hair, "
                           "garment construction, object geometry/material/color or location surfaces. Keep those "
                           "facts identical; follow THIS shot's camera, pose and action. Do not copy the old "
                           "composition or add other subjects from the reference. Matched entities: " + json.dumps(entities))
            emit("still_frame", f"Shot {number}: generating opening still with {len(references)} locked image reference(s).")
            feedback = ""
            aspect_ratio = result.get("aspect_ratio") or "16:9"
            for attempt in range(2):
                image = generate_still(visual, references, aspect_ratio, feedback)
                dimensions = check_dimensions(image, aspect_ratio)
                if not dimensions["matches"]:
                    feedback = (f"Decoded image is {dimensions['width']}x{dimensions['height']}; expected "
                                f"{aspect_ratio} within 2% ratio tolerance. Generate the correct aspect ratio.")
                    emit("still_frame", f"WARNING: Shot {number}: aspect-ratio mismatch. {feedback} "
                         + ("Retrying once." if attempt == 0 else "Using text fallback."))
                    if attempt == 1:
                        raise StillFrameError("Aspect-ratio mismatch after retry: " + feedback)
                    continue
                emit("still_frame", f"Shot {number}: decoded {dimensions['width']}x{dimensions['height']} "
                     f"matches {aspect_ratio} within 2% ratio tolerance.")
                verdict = check_still(visual, references, image, emit=emit, entities=entities)
                if verdict["approved"]:
                    break
                feedback = verdict["reason"]
                emit("still_frame", f"Shot {number}: visual check rejected candidate {attempt + 1}; "
                     + ("retrying once." if attempt == 0 else "using text fallback."))
            else:
                raise StillFrameError("Still-frame visual check rejected both candidates")
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[image.content_type]
            key = f"jobs/{job_id}/stills/{number}-{uuid.uuid4().hex}.{ext}"
            stored = storage_service.upload_bytes(key=key, body=image.data, content_type=image.content_type)
            shot.update(still_frame_url=stored["url"], still_frame_key=stored["key"],
                        still_frame_source_hash=shot_fingerprint(shot))
            observed = verdict.get("visible_entities", [])
            if not isinstance(observed, list):
                observed = []
            for entity_id in entities:
                if entity_id not in reference_map and entity_id in observed:
                    reference_map[entity_id] = {"name": entities[entity_id], "url": stored["url"],
                                                "key": stored["key"], "shot_number": number,
                                                "source_hash": shot_fingerprint(shot)}
                    reference_bytes[entity_id] = image
                    emit("still_frame", f"Shot {number}: established job-only reference for {entity_id}.")
                elif entity_id not in reference_map:
                    emit("still_frame", f"WARNING: Shot {number}: QA did not confirm visibility of {entity_id}; no job reference established.")
            emit("still_frame", f"Shot {number}: opening still passed visual check and was stored.")
        except Exception as error:
            detail = str(error) if isinstance(error, StillFrameError) else type(error).__name__
            warning = f"Still frame unavailable for shot {number}: {detail}. Continuing with compiled text; job is not blocked."
            shot["still_frame_warning"] = warning
            emit("still_frame", "WARNING: " + warning)
    return result["shots"]
