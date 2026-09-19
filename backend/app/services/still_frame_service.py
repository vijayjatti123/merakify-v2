"""Optional still previews after compilation; direct Google images, never video."""
import base64
import hashlib
import io
import json
import re
import unicodedata
import uuid
import copy
import queue
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageOps

from app.config import settings
from app.services import storage_service
from app.services.ad_direction import opening_cast
from app.services.preview_reference_cache import ReferenceCache
from app.services.character_image_service import GeneratedCharacterImage, _download_reference_image


class StillFrameError(RuntimeError):
    pass


class VerificationUnavailable(StillFrameError):
    pass


class StillProviderError(StillFrameError):
    def __init__(self, status, model):
        self.status = status
        self.model = model
        self.retryable = status in (408, 429) or status >= 500
        super().__init__(f"Google still-frame request failed (HTTP {status})")


class NoStillImageError(StillFrameError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        candidates = diagnostics.get("candidates", [])
        reasons = [c.get("finishReason", "unknown") for c in candidates]
        blocked = diagnostics.get("promptFeedback", {}).get("blockReason")
        self.retryable = bool(reasons) and all(r in {"IMAGE_OTHER", "NO_IMAGE"} for r in reasons) and not blocked and not any(
            rating.get("blocked") for c in candidates for rating in c.get("safetyRatings", [])) and not any(
            rating.get("blocked") for rating in diagnostics.get("promptFeedback", {}).get("safetyRatings", []))
        super().__init__("Google returned no usable still image; finish_reason=" + ",".join(reasons)
                         + ("; block_reason=" + str(blocked) if blocked else ""))


def _image_response_diagnostics(response, parts):
    """Retain diagnostic metadata, never candidate image bytes or request prose."""
    def message(value):
        text = str(value or "")
        for part in parts:
            if part.get("text"):
                text = text.replace(part["text"], "[request text omitted]")
        return re.sub(r"https?://\S+", "[URL omitted]", text)[:500]
    feedback = response.get("promptFeedback") or {}
    return {"model": settings.gemini_image_model, "modelVersion": response.get("modelVersion"),
        "responseId": response.get("responseId"), "usageMetadata": response.get("usageMetadata"),
        "promptFeedback": {k: feedback[k] for k in ("blockReason", "safetyRatings") if k in feedback},
        "candidates": [{"finishReason": c.get("finishReason") or "unknown",
                        "finishMessage": message(c.get("finishMessage")),
                        "safetyRatings": c.get("safetyRatings") or []}
                       for c in response.get("candidates", [])]}


# Allow provider resolution rounding (e.g. 1376x768 for 16:9), not wrong orientation.
ASPECT_RATIO_TOLERANCE = 0.02
MAX_REQUEST_IMAGES = 8
# Reserve QA's candidate slot in both paths so it checks the same references
# generation saw. This is a conservative budget, not the provider's maximum.
MAX_REFERENCE_IMAGES = MAX_REQUEST_IMAGES - 1


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
    if shot.get("preview_input"):
        return hashlib.sha256(json.dumps([shot["preview_input"], shot.get("preview_dependencies", {})], sort_keys=True).encode()).hexdigest()
    fields = ("compiled_prompt", "description", "camera_angle", "camera_movement",
              "lighting", "composition_note", "characters_in_shot", "dialogue_text")
    return hashlib.sha256(json.dumps({k: shot.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def invalidate_changed_stills(result):
    from app.services.preview_plan import preview_input
    for shot in result.get("shots", []):
        if shot.get("preview_input"):
            shot["preview_input"] = preview_input(result, shot)
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


VISUAL_CHECKS = ("identity_wardrobe", "placement_support", "props_contact",
                 "opening_state", "framing", "lighting_style")


def _google(parts, *, aspect_ratio=None, verification=False):
    if sum("inlineData" in part for part in parts) > MAX_REQUEST_IMAGES:
        raise StillFrameError("Internal still-frame image budget exceeded")
    if not settings.google_ai_api_key.strip():
        raise StillFrameError("Google image credentials are not configured")
    config = {"responseModalities": ["IMAGE" if aspect_ratio else "TEXT"]}
    if aspect_ratio:
        config["imageConfig"] = {"aspectRatio": aspect_ratio}
    else:
        config["responseMimeType"] = "application/json"
    model = settings.gemini_image_model
    if verification:
        model = settings.gemini_preview_check_model
        config["responseSchema"] = {"type": "OBJECT", "properties": {
            "approved": {"type": "BOOLEAN"}, "reason": {"type": "STRING"},
            "visible_entities": {"type": "ARRAY", "items": {"type": "STRING"}},
            "spatially_grounded": {"type": "BOOLEAN"},
            "visual_checks": {"type": "OBJECT", "properties": {key: {
                "type": "OBJECT", "properties": {
                    "status": {"type": "STRING", "enum": ["pass", "fail", "uncertain", "not_applicable"]},
                    "requirement": {"type": "STRING"}, "evidence": {"type": "STRING"}},
                "required": ["status", "requirement", "evidence"]} for key in VISUAL_CHECKS},
                "required": list(VISUAL_CHECKS)}},
            "required": ["approved", "reason", "visible_entities", "spatially_grounded", "visual_checks"]}
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model.strip(), safe='')}:generateContent",
        data=json.dumps({"contents": [{"parts": parts}], "generationConfig": config}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": settings.google_ai_api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except HTTPError as error:
        # Never put provider payloads, source prompts or signed URLs in shared traces.
        raise StillProviderError(error.code, model) from error


def _continuation_parts(continuation, *, checking=False):
    if continuation is None:
        return []
    number, image = continuation[:2]
    instruction = (
        f"Previous shot {number}'s actual accepted still — same-scene action context, not an identity sheet. "
        "This is an opening-frame preview, not a video end frame. Use its visible physical state as "
        "the starting reference for continuity: liquid level, active incoming stream, object positions "
        "and contact relationships. Carry an active action forward plausibly into the requested moment; "
        "do not silently replace it with an unrelated completed state, invent a large fill-level jump, "
        "or remove an active stream without support in the current shot. Follow the current shot's "
        "camera angle and framing rather than copying the previous composition. Existing vault and "
        "job-entity references remain authoritative for identity and design."
    )
    if checking:
        instruction += (" Reject clear unexplained state discontinuities relative to this reference, "
                        "while allowing the action's supported progression and the new camera viewpoint. "
                        "Do not require pixel-identical pose or composition.")
    return [{"text": instruction}, _inline(image)]


def _reference_parts(references, continuation, *, checking=False, emit=None):
    """Deduplicate source URLs, then select vault > newest entity > anchor.

    Production entries carry (label, image, source_url, priority, established_shot).
    Legacy two-tuples have no URL; byte identity is the conservative fallback.
    Distinct external URLs are not assumed to identify the same image.
    """
    entries = []
    for reference in references:
        name, image = reference[:2]
        source, priority, established = reference[2:] if len(reference) > 2 else (
            "sha256:" + hashlib.sha256(image.data).hexdigest(), 0, 0)
        prefix = "Identity reference: " if checking else "Locked identity reference for "
        entries.append((source, priority, established, name, image, prefix + name + ":"))
    if continuation:
        number, image = continuation[:2]
        source = continuation[2] if len(continuation) > 2 else "sha256:" + hashlib.sha256(image.data).hexdigest()
        entries.append((source, 2, 0, f"continuation shot {number}", image,
                        _continuation_parts(continuation, checking=checking)[0]["text"]))
    groups = {}
    for source, priority, established, name, image, instruction in sorted(entries, key=lambda e: (e[1], -e[2])):
        # Compare the original URL, before fresh_reference renews its signature.
        # Never print signed URLs in traces; labels identify omissions instead.
        if source not in groups:
            groups[source] = {"image": image, "names": [], "instructions": []}
        groups[source]["names"].append(name)
        groups[source]["instructions"].append(instruction)
    selected = list(groups.values())[:MAX_REFERENCE_IMAGES]
    dropped = list(groups.values())[MAX_REFERENCE_IMAGES:]
    if emit and (len(entries) != len(groups) or dropped):
        emit("still_frame", f"Reference budget ({'QA' if checking else 'generation'}): "
             f"{len(entries)} requested, {len(groups)} unique source URLs, {len(selected)} attached "
             f"(+{1 if checking else 0} candidate; total cap {MAX_REQUEST_IMAGES}). "
             f"{len(entries) - len(groups)} duplicate image attachment(s) merged with all labels retained. "
             + ("WARNING: Dropped references: " + "; ".join(
                 ", ".join(group["names"]) for group in dropped) if dropped else "No unique references dropped."))
    parts = []
    for group in selected:
        parts.extend([{"text": "\n".join(group["instructions"])}, _inline(group["image"])])
    return parts


def generate_still(visual, references, aspect_ratio, feedback="", *, continuation=None, emit=None):
    parts = _reference_parts(references, continuation, emit=emit)
    product_reference = any(len(ref) > 2 and ref[2].startswith("product:") for ref in references)
    parts.append({"text": (
        "Generate ONE still image: the opening frame of this compiled film shot. "
        "Freeze the first described physical instant; later motion and transition descriptions are "
        "context only, not additional panels or a later completed action. Preserve the specified "
        "framing, subject placement, lighting, palette and rendering style. Supplied reference images "
        "lock each named character's identity, face, hair and clothing; do not replace their face. "
        "Reference sheets are identity guides, never reproduce their layout. No collage, captions, "
        + ("invented logos, captions or audio. Preserve existing product packaging text/logos exactly as shown in approved product references.\nCompiled visual description:\n" if product_reference
           else "logos, readable text or audio.\nCompiled visual description:\n") + visual
        + ("\nCorrect the previous visual check: " + feedback if feedback else "")
    )})
    response = _google(parts, aspect_ratio=aspect_ratio)
    diagnostics = _image_response_diagnostics(response, parts)
    if emit:
        emit("still_provider_response", json.dumps(diagnostics))
    for candidate in response.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            data = part.get("inlineData", {})
            if data.get("data") and not part.get("thought"):
                raw = base64.b64decode(data["data"], validate=True)
                mime = data.get("mimeType", "")
                if raw and mime in {"image/png", "image/jpeg", "image/webp"}:
                    return GeneratedCharacterImage(raw, mime)
    raise NoStillImageError(diagnostics)


def check_still(visual, references, image, *, emit=None, entities=None, continuation=None):
    product_reference = any(len(ref) > 2 and ref[2].startswith("product:") for ref in references)
    parts = [{"text": (
        "Check this single opening-frame preview against the compiled visual description and any "
        "locked character references. Reject clear identity/outfit changes, wrong subject or framing, "
        + ("collages, invented captions, changed product branding/shape/packaging, or a later completed action. Existing text/logos on approved product packaging are required and must NOT be rejected as readable text. " if product_reference
           else "collages, readable text, or a later completed action instead of the described opening. ") +
        "Ignore motion/audio requirements that cannot be depicted in a still. Treat explicit "
        "spatial requirements as hard acceptance criteria. Compare inside/outside placement and "
        "support against the specified opening, respecting intentionally airborne/fantastical subjects. "
        "Do not demand new details absent from the description. Return visual_checks with exactly "
        + ", ".join(VISUAL_CHECKS) + ". For EACH category quote its requirement from the supplied "
        "opening description/reference label and describe actual visible evidence, not expected content. "
        "Check identity/clothing, containment and support, relative placement and prop ownership/contact, "
        "opening action (no later deployment/reveal), framing, and lighting/style respectively. "
        "Use status pass, fail, uncertain or not_applicable. If a required relationship cannot be seen "
        "because of occlusion/cropping/ambiguity, use uncertain, NEVER assume it passes. Do not require "
        "feet in every close-up: appropriate visible cabin/seat/threshold geometry can establish position. "
        "Use not_applicable only when no requirement/reference exists for that category; explain why. "
        "Report ALL failures together with specific corrective evidence. Approve only when every applicable "
        "check passes. Set spatially_grounded false for failed/uncertain placement_support.\n"
        + visual
    )}]
    if entities:
        parts.append({"text": "Also return visible_entities: an array of the exact entity IDs below that are "
                      "visibly identifiable in the candidate. Omit offscreen, merely mentioned, or too-cropped "
                      "entities. For job references compare ONLY the named entity's identity/design/material; "
                      "allow the new framing, pose, action and lighting. Reject changed faces, garments, prop "
                      "designs or location surfaces. Entity catalog: " + json.dumps(entities)})
    parts.extend(_reference_parts(references, continuation, checking=True, emit=emit))
    parts.extend([{"text": "Candidate opening frame to check:"}, _inline(image)])
    for attempt in range(2):
        try:
            response = _google(parts, verification=True)
            output = [p for c in response.get("candidates", [])
                      for p in c.get("content", {}).get("parts", []) if not p.get("thought")]
            content = "".join(p.get("text", "") for p in output)
            verdict = json.loads(content.strip().removeprefix("```json").removesuffix("```").strip())
            if verdict.get("approved") is True and verdict.get("reason") is None:
                verdict["reason"] = "Approved"
            if not isinstance(verdict.get("spatially_grounded"), bool):
                raise ValueError("Missing spatial verification")
            if not isinstance(verdict.get("approved"), bool) or not isinstance(verdict.get("reason"), str):
                raise ValueError("Invalid verification verdict")
            checks = verdict.get("visual_checks")
            if not isinstance(checks, dict) or set(checks) != set(VISUAL_CHECKS):
                raise ValueError("Missing visual checklist")
            failures = []
            for key in VISUAL_CHECKS:
                row = checks[key]
                if (not isinstance(row, dict) or row.get("status") not in
                        ("pass", "fail", "uncertain", "not_applicable") or
                        any(not isinstance(row.get(k), str) or not row[k].strip()
                            for k in ("requirement", "evidence"))):
                    raise ValueError("Invalid visual checklist evidence")
                if row["status"] in ("fail", "uncertain"):
                    failures.append(f"{key} ({row['status']}): {row['requirement']} — {row['evidence']}")
            if failures:
                verdict["approved"] = False
                verdict["reason"] = "; ".join(failures)
            if checks["placement_support"]["status"] in ("fail", "uncertain"):
                verdict["spatially_grounded"] = False
            if verdict.get("approved") and not verdict.get("spatially_grounded"):
                verdict["approved"] = False
                verdict["reason"] = "Spatial relationships do not match the opening state. " + verdict["reason"]
            if not verdict["approved"] and not verdict["reason"].strip():
                raise ValueError("Missing rejection reason")
            break
        except Exception as error:
            retryable = not isinstance(error, StillProviderError) or error.retryable
            if emit:
                # Status/model and exception type only: never provider bodies, prompts or URLs.
                emit("preview_verification_error", json.dumps({
                    "model": settings.gemini_preview_check_model, "attempt": attempt + 1,
                    "error_type": type(error).__name__,
                    "http_status": error.status if isinstance(error, StillProviderError) else None,
                    "retryable": retryable}))
                emit("still_frame", "Preview verification unavailable; " +
                     ("retrying verification of the same image once." if attempt == 0 and retryable else "candidate retained for verification retry."))
            if attempt == 1 or not retryable:
                raise VerificationUnavailable("Preview verification unavailable") from error

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
    visible = {" ".join(_words(n)) for n in opening_cast(shot)}
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


def _continuous_pair(previous, shot, result):
    """Conservative existing-data proxy, not a new action-state classifier.

    Adjacent shots must share a known scene and an ordinary/action-match cut.
    Cross-scene graphic matches, dissolves, unknown boundaries and explicit time
    jumps are not evidence of one continuous action. Same-scene cuts can still
    switch subjects/actions: the current shot remains authoritative in that case.
    """
    if previous is None or shot.get("scene_number") is None:
        return False
    if previous.get("scene_number") != shot["scene_number"]:
        return False
    key = f"{previous['shot_number']}-{shot['shot_number']}"
    boundary = next((b for b in result.get("assembly", {}).get("transitions", [])
                     if b.get("between") == key), None)
    return bool(boundary and boundary.get("type") in {"cut", "match cut"} and not
                re.search(r"later|time[- ]?(?:jump|passage)|flashback|next day", boundary.get("reason") or "", re.I))


def _generate_still_frames_serial(result, *, job_id, emit, shot_numbers=None, on_progress=None, feedback_by_shot=None, reference_cache=None, on_accepted=None):
    """Keep the plan reviewable, but explicitly mark missing output as failed."""
    characters = {c["name"].strip().casefold(): c for c in result.get("continuity", {}).get("characters", [])}
    # References live only in this job's result JSON, pointing to its own stills.
    # Rebuild on each full still pass so revised plans cannot inherit stale anchors.
    reference_map = result.setdefault("entity_references", {}) if shot_numbers is not None else {}
    result["entity_references"] = reference_map
    cache = reference_cache if reference_cache is not None else ReferenceCache()
    def reference(url, number):
        started = time.monotonic()
        def load():
            downloaded = _download_reference_image(fresh_reference(url))
            emit("preview_timing", json.dumps({"shot_number": number, "phase": "reference_download",
                "elapsed_sec": round(time.monotonic() - started, 3), "bytes": len(downloaded.data)}))
            return downloaded
        image, hit = cache.get(url, load)
        emit("preview_timing", json.dumps({"shot_number": number, "phase": "reference_ready",
            "cache_hit": hit, "elapsed_sec": round(time.monotonic() - started, 3)}))
        return image
    from app.db import SessionLocal
    from app.services.product_service import job_references
    with SessionLocal() as product_db:
        products = job_references(product_db, job_id)
    ordered = sorted(result.get("shots", []), key=lambda s: s["shot_number"])
    previous_image = None
    # This loop was already synchronous for Module P. Keep generation, QA and
    # upload inside it: the next iteration may consume only the accepted image
    # that produced the preceding shot's stored URL, never an in-flight candidate.
    for index, shot in enumerate(ordered):
        if shot_numbers is not None and shot["shot_number"] not in shot_numbers:
            previous_image = None
            continue
        previous = ordered[index - 1] if index else None
        continuation = None
        if _continuous_pair(previous, shot, result):
            if shot_numbers is not None and previous.get("still_frame_url"):
                try:
                    previous_image = reference(previous["still_frame_url"], shot["shot_number"])
                except Exception:
                    emit("still_frame", f"WARNING: Shot {shot['shot_number']}: previous still could not be loaded; no action anchor used.")
            if previous.get("still_frame_url") and previous_image is not None:
                continuation = (previous['shot_number'], previous_image, previous['still_frame_url'])
            else:
                emit("still_frame", f"Shot {shot['shot_number']}: previous shot {previous['shot_number']} "
                     "has no accepted still; continuing without an action anchor.")
        previous_image = None  # A skipped/failed shot must break the anchor chain.
        shot["still_frame_url"] = None
        shot.pop("still_frame_key", None)
        shot.pop("still_frame_display", None)
        shot.pop("still_frame_source_hash", None)
        shot.pop("still_frame_warning", None)
        shot.pop("still_frame_error_kind", None)
        shot["still_frame_status"] = "pending"
        if not shot.get("compiled_prompt") and not shot.get("preview_input"):
            continue  # Dialogue jobs wait for real post-approval compilation.
        number = shot["shot_number"]
        shot["still_frame_status"] = "generating"
        if on_progress:
            on_progress(result)
        try:
            from app.services.preview_plan import preview_visual
            visual = preview_visual(shot["preview_input"]) if shot.get("preview_input") else visual_description(shot["compiled_prompt"])
            if (feedback_by_shot or {}).get(number):
                visual += "\nRequested image adjustment (preserve locked identity and style): " + feedback_by_shot[number]
            entities = match_entities(result, shot, visual)
            references = []
            if products:
                shot["approved_product_references"] = products
            for name in opening_cast(shot):
                character = characters.get(name.strip().casefold(), {})
                if character.get("character_id"):
                    if not character.get("image_url"):
                        raise StillFrameError("Locked character has no reference image")
                    references.append((name, reference(character["image_url"], number),
                                       character["image_url"], 0, 0))
            for product in products:
                key = product["object_key"]
                product_image = reference(storage_service.asset_url(key), number)
                references.append(("Product " + product["name"] + "; preserve packaging, geometry, color, logo and printed text. Use only when this shot calls for the product; never copy the reference layout or unrelated props",
                                   product_image, "product:" + key, 0, 0))
                emit("still_frame", f"Shot {number}: approved product reference {product['product_id']} attached for generation and QA.")
            if products:
                visual += "\nApproved product images lock product identity, NOT this scene's framing. Do not insert a product into a shot that does not call for it."
                if len(references) > MAX_REFERENCE_IMAGES:
                    raise StillFrameError("Too many locked character/product references for this shot; reduce the selected references.")
            for entity_id in entities:
                if entity_id in reference_map and reference_map[entity_id]["shot_number"] < number:
                    entity_image = reference(reference_map[entity_id]["url"], number)
                    references.append(("Job entity " + entity_id + "; preserve only this entity, not the old shot layout",
                                       entity_image, reference_map[entity_id]["url"],
                                       1, reference_map[entity_id]["shot_number"]))
                    emit("still_frame", f"Shot {number}: conditioning {entity_id} from job reference shot {reference_map[entity_id]['shot_number']}.")
            if entities:
                visual += ("\nJob-scoped consistency: reference frames lock ONLY each labeled entity's face, hair, "
                           "garment construction, object geometry/material/color or location surfaces. Keep those "
                           "facts identical; follow THIS shot's camera, pose and action. Do not copy the old "
                           "composition or add other subjects from the reference. Matched entities: " + json.dumps(entities))
            emit("still_frame", f"Shot {number}: considering {len(references)} locked image reference(s) before budget selection.")
            if continuation:
                emit("still_frame", f"Shot {number}: considering previous-shot action anchor from shot {continuation[0]} "
                     f"subject to reference budget (image SHA256 {hashlib.sha256(continuation[1].data).hexdigest()}).")
            feedback = ""
            aspect_ratio = result.get("aspect_ratio") or "16:9"
            verification_source = hashlib.sha256(json.dumps([visual, aspect_ratio,
                [hashlib.sha256(r[1].data).hexdigest() for r in references],
                hashlib.sha256(continuation[1].data).hexdigest() if continuation else None], sort_keys=True).encode()).hexdigest()
            candidate = shot.get("still_frame_candidate")
            if candidate and candidate.get("source") != verification_source:
                shot.pop("still_frame_candidate", None)
                candidate = None
            for attempt in range(candidate.get("attempt", 0) if candidate else 0, 2):
                started = time.monotonic()
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "image_request_started", "attempt": attempt + 1}))
                def attempt_emit(key, note):
                    if key == "still_provider_response":
                        note = json.dumps({**json.loads(note), "shot_number": number, "attempt": attempt + 1})
                    emit(key, note)
                try:
                    if candidate:
                        try:
                            image = reference(storage_service.asset_url(candidate["key"]), number)
                        except Exception as error:
                            raise VerificationUnavailable("Saved preview could not be loaded for verification") from error
                    else:
                        image = generate_still(visual, references, aspect_ratio, feedback, continuation=continuation, emit=attempt_emit)
                except NoStillImageError as error:
                    if not error.retryable or attempt == 1:
                        raise
                    # Share the existing two-candidate budget with QA correction;
                    # never stack a new retry loop or alter references to bypass a block.
                    shot["still_frame_retrying"] = True
                    if on_progress:
                        on_progress(result)
                    emit("preview_timing", json.dumps({"shot_number": number, "phase": "automatic_retry",
                        "attempt": 1, "next_attempt": 2, "elapsed_sec": round(time.monotonic() - started, 3),
                        "delay_sec": 2, "reason": "Provider returned no image; retrying the unchanged request once."}))
                    time.sleep(2)
                    continue
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "image_request_completed", "attempt": attempt + 1,
                    "elapsed_sec": round(time.monotonic() - started, 3)}))
                decoded_at = time.monotonic()
                dimensions = check_dimensions(image, aspect_ratio)
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "image_decode",
                    "attempt": attempt + 1, "elapsed_sec": round(time.monotonic() - decoded_at, 3)}))
                if not dimensions["matches"]:
                    feedback = (f"Decoded image is {dimensions['width']}x{dimensions['height']}; expected "
                                f"{aspect_ratio} within 2% ratio tolerance. Generate the correct aspect ratio.")
                    emit("still_frame", f"WARNING: Shot {number}: aspect-ratio mismatch. {feedback} "
                         + ("Retrying once." if attempt == 0 else "No still accepted; regeneration needed."))
                    if attempt == 1:
                        raise StillFrameError("Aspect-ratio mismatch after retry: " + feedback)
                    continue
                emit("still_frame", f"Shot {number}: decoded {dimensions['width']}x{dimensions['height']} "
                     f"matches {aspect_ratio} within 2% ratio tolerance.")
                started = time.monotonic()
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "visual_check_started", "attempt": attempt + 1}))
                try:
                    verdict = check_still(visual, references, image, emit=emit, entities=entities, continuation=continuation)
                except Exception as error:
                    if not candidate:
                        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[image.content_type]
                        key = f"jobs/{job_id}/preview-candidates/{number}-{uuid.uuid4().hex}.{ext}"
                        storage_service.upload_bytes(key=key, body=image.data, content_type=image.content_type,
                                                    cache_control="private, max-age=300")
                        shot["still_frame_candidate"] = {"key": key, "source": verification_source, "attempt": attempt}
                    raise VerificationUnavailable("Image created, but verification is unavailable") from error
                shot.pop("still_frame_candidate", None)
                candidate = None
                shot["still_frame_verification"] = verdict
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "visual_check_completed", "attempt": attempt + 1,
                    "elapsed_sec": round(time.monotonic() - started, 3), "approved": verdict["approved"]}))
                if verdict["approved"]:
                    if continuation:
                        emit("still_frame", f"Warning — shot {number}: physical-state QA approval is a model judgment, "
                             "not proof of an unseen event. A missing stream does not establish that a pour concluded; "
                             "review the visible state against the explicit planned boundary.")
                    break
                feedback = verdict["reason"]
                emit("still_frame", f"Shot {number}: visual check rejected candidate {attempt + 1}; "
                     + ("retrying once." if attempt == 0 else "no still accepted; regeneration needed.")
                     + f" Reason: {feedback}")
            else:
                raise StillFrameError("Still-frame visual check rejected both candidates")
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[image.content_type]
            key = f"jobs/{job_id}/stills/{number}-{uuid.uuid4().hex}.{ext}"
            started = time.monotonic()
            stored = storage_service.upload_bytes(key=key, body=image.data, content_type=image.content_type,
                cache_control="private, max-age=300")
            emit("preview_timing", json.dumps({"shot_number": number, "phase": "stored", "elapsed_sec": round(time.monotonic() - started, 3)}))
            shot.update(still_frame_url=stored["url"], still_frame_key=stored["key"],
                        still_frame_source_hash=shot_fingerprint(shot), still_frame_status="ready")
            if on_accepted:
                on_accepted(number, image, stored["key"])
            # Reuse the exact bytes uploaded at this URL; no redundant S3 download.
            previous_image = image
            cache.seed(stored["url"], image)
            observed = verdict.get("visible_entities", [])
            if not isinstance(observed, list):
                observed = []
            for entity_id in entities:
                if entity_id not in reference_map and entity_id in observed:
                    reference_map[entity_id] = {"name": entities[entity_id], "url": stored["url"],
                                                "key": stored["key"], "shot_number": number,
                                                "source_hash": shot_fingerprint(shot)}
                    cache.seed(stored["url"], image)
                    emit("still_frame", f"Shot {number}: established job-only reference for {entity_id}.")
                elif entity_id not in reference_map:
                    emit("still_frame", f"WARNING: Shot {number}: QA did not confirm visibility of {entity_id}; no job reference established.")
            emit("still_frame", f"Shot {number}: opening still passed visual check and was stored.")
        except Exception as error:
            detail = str(error) if isinstance(error, StillFrameError) else type(error).__name__
            warning = f"Shot {number}: This shot couldn't be generated — try regenerating it. No still was accepted. Reason: {detail}."
            shot["still_frame_status"] = "failed"
            shot["still_frame_error_kind"] = "verification" if isinstance(error, VerificationUnavailable) else "generation"
            if isinstance(error, VerificationUnavailable):
                warning = "Your image is saved, but we couldn't verify it. Retry verification to check the same image."
            shot["still_frame_warning"] = warning
            emit("still_frame", "WARNING: " + warning)
        shot.pop("still_frame_retrying", None)
        if on_progress:
            on_progress(result)
    return result["shots"]


def dependency_priority(ordered, deps, pending):
    """Prefer ready work that unlocks more pending work; never bypass an edge."""
    def descendants(number):
        reached, frontier = set(), {number}
        while frontier:
            children = {n for n in pending if deps.get(n, set()) & frontier} - reached - {number}
            reached.update(children)
            frontier = children
        return len(reached)
    return sorted(ordered, key=lambda s: (-descendants(s['shot_number']), s['shot_number']))


def generate_still_frames(result, *, job_id, emit, shot_numbers=None, on_progress=None, feedback_by_shot=None):
    """Two bounded workers; only the owning thread persists progress or emits DB events.

    Potential entity seeders serialize until QA establishes a canonical reference;
    continuous action anchors remain sequential. Each worker receives a snapshot,
    never the owner's mutable plan or DB session.
    """
    from app.services.preview_plan import preview_input, preview_visual
    ordered = sorted(result.get("shots", []), key=lambda s: s["shot_number"])
    targets = {s["shot_number"] for s in ordered if shot_numbers is None or s["shot_number"] in shot_numbers}
    for shot in ordered:
        facts = preview_input(result, shot)
        if facts:
            shot["preview_input"] = facts
    invalidate_changed_stills(result)
    references = result.setdefault("entity_references", {})
    def dependency_graph():
        deps, previous_entities = {}, {}
        for index, shot in enumerate(ordered):
            number = shot["shot_number"]
            visual = preview_visual(shot["preview_input"]) if shot.get("preview_input") else visual_description(shot["compiled_prompt"]) if shot.get("compiled_prompt") else ""
            dependencies = set()
            for entity in match_entities(result, shot, visual):
                anchor = references.get(entity, {}).get("shot_number")
                if anchor is not None and anchor < number:
                    dependencies.add(anchor)
                elif entity in previous_entities:
                    # Until QA establishes an anchor, serialize potential seeders.
                    # A failed/unseen seed cannot license ungrounded parallelism.
                    dependencies.add(previous_entities[entity])
                previous_entities[entity] = number
            if index and _continuous_pair(ordered[index - 1], shot, result):
                dependencies.add(ordered[index - 1]["shot_number"])
            deps[number] = dependencies
        return deps
    deps = dependency_graph()
    cache = ReferenceCache()
    pending = set(targets)
    done, futures = set(), {}
    events = queue.Queue()
    by_number = {s["shot_number"]: s for s in ordered}
    def dependencies_for(number):
        return {str(n): by_number[n].get("still_frame_key") or by_number[n].get("still_frame_url")
                for n in sorted(deps[number])}
    def persist():
        if on_progress:
            on_progress(result)
    for shot in ordered:
        number = shot["shot_number"]
        if number not in pending:
            continue
        if not shot.get("still_frame_url"):
            shot["still_frame_status"] = "pending"
    persist()
    display_tasks = queue.Queue()
    def display_job(number, image, key):
        from app.services.preview_display import store_variants
        try:
            return store_variants(image, key, emit=lambda k, n: events.put((k, n)), shot_number=number)
        except Exception as error:
            events.put(("still_frame", f"Shot {number}: display derivative unavailable ({type(error).__name__}); original retained."))
            return None
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview-display") as display_pool, \
         ThreadPoolExecutor(max_workers=2, thread_name_prefix="still-preview") as pool:
        def accepted(number, image, key):
            display_tasks.put((number, key, display_pool.submit(display_job, number, image, key)))
        while pending or futures:
            pending_before = len(pending)
            deps = dependency_graph()
            for shot in dependency_priority(ordered, deps, pending):
                number = shot["shot_number"]
                if len(futures) >= 2:
                    break
                if number not in pending or not (deps[number] & targets) <= done:
                    continue
                pending.remove(number)
                shot["preview_dependencies"] = dependencies_for(number)
                if shot.get("still_frame_url") and shot.get("still_frame_source_hash") == shot_fingerprint(shot):
                    done.add(number)
                    emit("still_frame", f"Shot {number}: unchanged accepted preview reused.")
                    continue
                shot["still_frame_status"] = "generating"
                shot["still_frame_url"] = None
                shot.pop("still_frame_key", None)
                shot.pop("still_frame_display", None)
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "dependencies_ready",
                    "dependencies": sorted(deps[number])}))
                persist()
                snapshot = copy.deepcopy(result)
                started = time.monotonic()
                def work(snapshot=snapshot, number=number):
                    _generate_still_frames_serial(snapshot, job_id=job_id,
                        emit=lambda k, n: events.put((k, n)), shot_numbers={number}, feedback_by_shot=feedback_by_shot, reference_cache=cache, on_accepted=accepted)
                    return snapshot
                futures[pool.submit(work)] = (number, started)
            if not futures:
                if pending:
                    if len(pending) == pending_before:
                        raise StillFrameError("Preview dependency graph cannot advance")
                    continue  # Reused upstream previews may have unlocked work.
                break
            complete, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
            while not events.empty():
                emit(*events.get_nowait())
            for future in complete:
                number, started = futures.pop(future)
                snapshot = future.result()
                rendered = next(s for s in snapshot["shots"] if s["shot_number"] == number)
                target = next(s for s in ordered if s["shot_number"] == number)
                target.clear()
                target.update(rendered)
                for entity, ref in snapshot.get("entity_references", {}).items():
                    if ref["shot_number"] == number:
                        references[entity] = ref
                done.add(number)
                persist()
                emit("preview_timing", json.dumps({"shot_number": number, "phase": "completed",
                    "elapsed_sec": round(time.monotonic() - started, 3), "status": target.get("still_frame_status")}))
        while not display_tasks.empty():
            number, key, future = display_tasks.get_nowait()
            display = future.result()
            if display and by_number[number].get("still_frame_key") == key:
                by_number[number]["still_frame_display"] = display
        while not events.empty():
            emit(*events.get_nowait())
        persist()
    return result["shots"]
