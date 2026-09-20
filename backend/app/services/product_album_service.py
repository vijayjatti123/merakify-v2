"""Reusable product views; durable, bounded asset work, not a Director pipeline."""
import base64
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models import ProductView
from app.services import storage_service as storage, product_service as products

ANGLES = ("front", "three_quarter", "side", "back", "top", "detail")
ANGLE_REQUIREMENTS = {
    "front": "Camera level with the product and centered on its front face; front plane square to camera with no visible yaw.",
    "three_quarter": "Camera level with the product; rotate it 25-35 degrees so the front and one side are visible while the front label remains readable.",
    "side": "Camera level with the product at a 90-degree profile; show the complete side silhouette without turning it into a rear view.",
    "back": "Camera level with the product at a 180-degree rear view; show the complete back surface. Do not copy front-label text onto the back.",
    "top": "Camera directly overhead, optical axis perpendicular to the top surface; show the complete top outline without an oblique front view.",
    "detail": "Use a close crop of a real detail that is visibly supported by the references; keep enough surrounding structure to locate it on the product.",
}
# Published fal 2K output estimates checked 2026-09-19; not actual billed cost.
MODELS = {"pro": ("fal-ai/nano-banana-pro/edit", .15),
          "standard": ("fal-ai/nano-banana-2/edit", .12)}
ACTIVE = ("queued", "submitting", "processing", "verifying")
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="product-album")
_futures = set()


def options():
    return {"angles": list(ANGLES), "models": [{"id": k, "label": "Nano Banana Pro" if k == "pro" else "Nano Banana 2",
        "estimate_per_image_usd": v[1], "max_generation_usd_per_view": round(v[1] * 2, 2)} for k, v in MODELS.items()],
        "estimate_note": "2K generation estimate, including at most one corrective image per view. Verification charges are additional; provider prices may change."}


def public(row):
    return {"id": row.id, "angle": row.angle, "provenance": row.provenance,
        "status": row.status, "error": row.error, "attempts": row.attempts,
        "model": row.model, "verdict": row.verdict,
        "generation_requirements": contract(row) if row.provenance == "inferred" else None,
        "url": storage.asset_url(row.object_key) if row.object_key else None,
        "thumbnail_url": storage.asset_url(row.thumbnail_key) if row.thumbnail_key else None}


def rows(db, product_id):
    return db.query(ProductView).filter_by(product_id=product_id).order_by(ProductView.created_at).all()


def approved_snapshot(db, product_id):
    return [{"id": r.id, "angle": r.angle, "provenance": r.provenance, "object_key": r.object_key}
            for r in rows(db, product_id) if r.status == "approved" and r.object_key]


def view_contract(angle, source_count):
    """The one persisted contract shared by image creation and verification."""
    return {
        "contract_version": "product-view-v1",
        "requested_view": ANGLE_REQUIREMENTS[angle],
        "geometry": "Preserve the exact silhouette, proportions, seams, closures, handles and structural parts visible in the supplied references.",
        "branding": "Preserve visible logo, lettering, label layout and certification marks exactly. Never invent, rewrite, mirror or relocate text.",
        "color_material": "Preserve the product's visible colors, transparency, finish and material cues; studio relighting may change illumination but not the product.",
        "clean_background": "Exactly one product, fully supported by a neutral white or light-grey seamless studio background, with a soft physically plausible contact shadow; no props, collage, hands or duplicate product.",
        "reference_policy": (f"Use all {source_count} supplied images as evidence of the same product. Reference 1 is the approved master. "
            "A surface not visible in any reference is unknown: infer only plain continuation of supported geometry/material, add no unseen text, logo, mechanism or claim."),
    }


def contract(row):
    return row.generation_contract or view_contract(row.angle, len(row.source_keys or []))


def generation_prompt(row):
    specification = contract(row)
    prompt = ("Create one professional 2K studio product photograph that satisfies this exact checklist. "
        "The checklist is authoritative for both generation and later verification.\n" +
        json.dumps(specification, ensure_ascii=False, sort_keys=True))
    if row.verdict:
        prompt += ("\nCorrect only the failed or uncertain checklist evidence below while preserving all other requirements:\n" +
                   json.dumps(row.verdict, ensure_ascii=False, sort_keys=True))
    return prompt


def verification_instruction(row):
    return ("Verify the candidate ONLY against the exact generation checklist below and the supplied source images. "
        "Do not invent additional aesthetic requirements. Never claim an unseen surface is verified: use uncertain "
        "when the candidate may be plausible but source evidence cannot confirm it. Fail observable contradictions. "
        "Return exactly these five categories: geometry, branding, color_material, requested_view and clean_background, "
        "each with status pass/fail/uncertain and concrete visible evidence. Contract version and reference policy guide "
        "the check but are not output categories.\n" +
        json.dumps(contract(row), ensure_ascii=False, sort_keys=True))


def save_image(product_id, view_id, image, suffix):
    key = f"products/{product_id}/views/{view_id}/{suffix}.png"
    thumb = key.removesuffix(".png") + ".webp"
    storage.upload_bytes(key, products.png(image), content_type="image/png")
    small = image.copy(); small.thumbnail((480, 480))
    out = io.BytesIO(); small.save(out, format="WEBP", quality=85)
    storage.upload_bytes(thumb, out.getvalue(), content_type="image/webp")
    return key, thumb


def upload(db, product, angle, data):
    if angle not in ANGLES:
        raise ValueError("Choose a supported product angle")
    if len(rows(db, product.id)) >= 24:
        raise ValueError("This album has 24 views. Reuse its approved views.")
    image = products.decode_image(data)
    row = ProductView(id=str(uuid.uuid4()), product_id=product.id, request_key=str(uuid.uuid4()),
        angle=angle, provenance="original", status="review")
    row.object_key, row.thumbnail_key = save_image(product.id, row.id, image, "original")
    db.add(row); db.commit()
    return row


def queue(db, product, angles, model, request_key, maximum):
    if model not in MODELS or not angles or len(angles) > 3 or len(set(angles)) != len(angles) or set(angles) - set(ANGLES):
        raise ValueError("Choose up to three distinct product views and a supported model")
    if product.status != "approved" or not product.accepted_key:
        raise ValueError("Approve the product master before preparing album views")
    cost = round(len(angles) * MODELS[model][1] * 2, 2)
    if maximum < cost:
        raise ValueError("Review the current generation estimate before continuing")
    # One batch identity prevents double clicks/network retries from charging twice.
    # Serialize album preparation per product, including requests with different keys.
    db.query(products.Product).filter_by(id=product.id).update({"name": products.Product.name})
    existing = db.query(ProductView).filter(ProductView.request_key.startswith(f"{request_key}:")).all()
    if existing:
        if {r.angle for r in existing} != set(angles) or any(r.product_id != product.id or r.model != model for r in existing):
            raise ValueError("This preparation request changed; refresh the album")
        db.commit()
        return existing
    current = rows(db, product.id)
    if len(current) + len(angles) > 24:
        raise ValueError("An album supports up to 24 views")
    if any(r.status in ACTIVE for r in current):
        raise ValueError("Let the current views finish before preparing more")
    # Never recursively generate from an inferred angle. Original photos + master only.
    source_keys = list(dict.fromkeys([product.accepted_key, product.crop_key] +
        [r.object_key for r in current if r.provenance == "original" and r.status == "approved"]))[:6]
    result = [ProductView(product_id=product.id, request_key=f"{request_key}:{angle}", angle=angle,
        provenance="inferred", status="queued", model=model, source_keys=source_keys,
        generation_contract=view_contract(angle, len(source_keys))) for angle in angles]
    db.add_all(result)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(ProductView).filter(ProductView.request_key.startswith(f"{request_key}:")).all()
        if ({r.angle for r in existing} == set(angles) and
                all(r.product_id == product.id and r.model == model for r in existing)):
            return existing
        raise ValueError("This preparation request could not be saved; refresh the album") from None
    return result


def approve(db, product_id, ids, inferred_ack=False):
    selected = [db.get(ProductView, ident) for ident in ids]
    if not selected or any(r is None or r.product_id != product_id or not r.object_key or r.status not in ("review", "approved") for r in selected):
        raise ValueError("Only reviewed views from this product can be approved")
    if any(r.provenance == "inferred" for r in selected) and not inferred_ack:
        raise ValueError("Confirm you reviewed the AI-inferred product details")
    for row in selected:
        row.status = "approved"
    db.commit()


def withdraw(db, row):
    changed = db.query(ProductView).filter_by(id=row.id, status="approved").update({"status": "review"})
    if not changed:
        raise ValueError("Only an approved view can be removed from future jobs")
    db.commit()  # Existing JobProduct snapshots intentionally remain unchanged.


def retry_verification(db, row):
    if row.status != "verification_unavailable" or not row.object_key:
        raise ValueError("There is no saved candidate awaiting verification")
    db.query(ProductView).filter_by(id=row.id, status="verification_unavailable").update(
        {"status": "verifying", "verification_only": True, "error": None, "lease_until": None, "next_poll_at": None})
    db.commit()


def queue_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "queue.fal.run" or not parsed.path.startswith("/fal-ai/"):
        raise ValueError("Invalid provider queue URL")
    return url


def fal(row, suffix="", payload=None, url=None):
    if not settings.fal_api_key:
        raise ValueError("Image generation is not configured")
    endpoint = MODELS[row.model][0]
    with httpx.Client(timeout=45) as client:
        response = client.request("POST" if payload is not None else "GET", queue_url(url or "https://queue.fal.run/" + endpoint + suffix),
            headers={"Authorization": "Key " + settings.fal_api_key}, **({"json": payload} if payload is not None else {}))
        response.raise_for_status()
        return response.json()


def verify(row):
    from app.services.character_image_service import _download_reference_image
    from app.services.still_frame_service import _inline
    checks = ("geometry", "branding", "color_material", "requested_view", "clean_background")
    parts = [{"text": verification_instruction(row)}]
    for number, key in enumerate(row.source_keys, 1):
        parts.extend([{"text": f"Source reference {number} ({'approved master' if number == 1 else 'approved original view'}):"},
                      _inline(_download_reference_image(storage.asset_url(key)))])
    parts.extend([{"text": "Candidate (may contain inferred surfaces):"},
        _inline(_download_reference_image(storage.asset_url(row.object_key)))])
    schema = {"type": "OBJECT", "properties": {k: {"type": "OBJECT", "properties": {
        "status": {"type": "STRING", "enum": ["pass", "fail", "uncertain"]}, "evidence": {"type": "STRING"}},
        "required": ["status", "evidence"]} for k in checks}, "required": list(checks)}
    with httpx.Client(timeout=45) as client:
        response = client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_preview_check_model}:generateContent",
            headers={"x-goog-api-key": settings.google_ai_api_key}, json={"contents": [{"parts": parts}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}})
        response.raise_for_status()
        text = "".join(p.get("text", "") for c in response.json().get("candidates", [])
            for p in c.get("content", {}).get("parts", []) if not p.get("thought"))
    verdict = json.loads(text)
    if set(verdict) != set(checks) or any(not isinstance(v, dict) or v.get("status") not in ("pass", "fail", "uncertain")
        or not isinstance(v.get("evidence"), str) for v in verdict.values()):
        raise ValueError("Invalid product verification")
    return verdict


def process(ident):
    with SessionLocal() as db:
        now = datetime.utcnow()
        claimed = db.query(ProductView).filter(ProductView.id == ident, ProductView.status.in_(ACTIVE),
            or_(ProductView.lease_until.is_(None), ProductView.lease_until < now)).update(
                {"lease_until": now + timedelta(minutes=5)}, synchronize_session=False)
        db.commit()
        if not claimed:
            return
        row = db.get(ProductView, ident, populate_existing=True)
        try:
            if row.status == "submitting":
                row.status = "failed"
                row.error = "Submission could not be confirmed after interruption. No automatic paid resubmission was made."
            elif row.status == "queued":
                row.status = "submitting"; row.attempts += 1; row.started_at = now
                db.commit()  # Crash here is uncertain; never resubmit silently.
                task = fal(row, payload={"prompt": generation_prompt(row), "image_urls": [storage.asset_url(k) for k in row.source_keys],
                    "resolution": "2K", "aspect_ratio": "1:1", "num_images": 1, "output_format": "png", "sync_mode": True})
                row.request_id = task["request_id"]
                row.status_url = queue_url(task["status_url"])
                row.response_url = queue_url(task["response_url"])
                row.status = "processing"
            elif row.status == "processing":
                path = "/requests/" + row.request_id
                status = fal(row, path + "/status", url=row.status_url)["status"]
                if status == "COMPLETED":
                    result = fal(row, path, url=row.response_url)
                    url = result["images"][0]["url"]
                    if not url.startswith("data:image/"):
                        raise ValueError("Expected inline product image")
                    data = base64.b64decode(url.split(",", 1)[1], validate=True)
                    row.object_key, row.thumbnail_key = save_image(row.product_id, row.id, products.decode_image(data), f"candidate-{row.attempts}")
                    row.candidate_keys = [*row.candidate_keys, row.object_key]
                    row.status = "verifying"; row.error = None
                elif status not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise ValueError("Provider failed")
                elif row.started_at and now - row.started_at > timedelta(minutes=10):
                    row.error = "The provider is taking longer than expected. This task is still being checked; no duplicate was submitted."
            elif row.status == "verifying":
                row.verdict = verify(row)
                failed = any(v["status"] == "fail" for v in row.verdict.values())
                if failed and row.attempts < 2 and not row.verification_only:
                    row.status = "queued"; row.request_id = None
                    row.status_url = None; row.response_url = None
                    row.error = "Correcting a visible mismatch (one included corrective attempt)."
                else:
                    row.status = "rejected" if failed else "review"
                    row.error = "Product details did not match. Upload a real view or explicitly prepare a new one." if failed else None
        except (httpx.RequestError, httpx.HTTPStatusError) as error:
            if row.status == "processing" and (not isinstance(error, httpx.HTTPStatusError) or error.response.status_code == 429 or error.response.status_code >= 500):
                row.error = "Progress check temporarily unavailable; the existing provider task is saved."
            else:
                row.status = "verification_unavailable" if row.status == "verifying" else "failed"
                row.error = "Image saved. Retry verification without regenerating." if row.object_key and row.status == "verification_unavailable" else "Generation could not finish. No unconfirmed request will be resubmitted automatically."
        except Exception:
            row.status = "verification_unavailable" if row.status == "verifying" else "failed"
            row.error = "Image saved. Retry verification without regenerating." if row.status == "verification_unavailable" else "Image generation could not finish. Your original references are safe."
        finally:
            row.lease_until = None; row.next_poll_at = datetime.utcnow() + timedelta(seconds=10)
            db.commit()


def dispatch():
    """Called by the existing durable dispatcher; at most two album operations."""
    global _futures
    _futures = {f for f in _futures if not f.done()}
    free = 2 - len(_futures)
    if free <= 0:
        return
    now = datetime.utcnow()
    with SessionLocal() as db:
        pending = db.query(ProductView.id).filter(ProductView.status.in_(ACTIVE),
            or_(ProductView.lease_until.is_(None), ProductView.lease_until < now),
            or_(ProductView.next_poll_at.is_(None), ProductView.next_poll_at < now)).order_by(ProductView.created_at).limit(free).all()
    for (ident,) in pending:
        _futures.add(POOL.submit(process, ident))


def relevant_views(product, shot, opening_only=False):
    """Pick at most one useful alternate, never send the entire album."""
    from app.services.video_references import mentions, shot_text
    text = str(shot.get("state_at_shot_start") or shot.get("description", "")) if opening_only else shot_text(shot)
    if not mentions(text, product["name"]):
        return []
    aliases = {"back": ("back label", "rear label", "rear view", "back of", "rear of"), "top": ("top-down", "overhead", "from above"),
        "side": ("side view", "profile view"), "three_quarter": ("three-quarter", "three quarter"), "detail": ("macro", "label close-up", "detail view")}
    text = (text + " " + str(shot.get("camera_angle", ""))).casefold()
    angle = next((k for k, words in aliases.items() if any(w in text for w in words)), None)
    views = [v for v in product.get("views", []) if v.get("angle") == angle and v.get("object_key")]
    return sorted(views, key=lambda v: v.get("provenance") != "original")[:1]
