"""Reusable, explicitly approved product references. No character/voice records."""
import json
import base64
import io
import uuid
from datetime import datetime

import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.models import Product, JobProduct
from app.services import storage_service


def decode_image(data):
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("Choose an image smaller than 15 MB.")
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in {"PNG", "JPEG", "WEBP"} or getattr(im, "is_animated", False):
            raise ValueError()
        if im.width * im.height > 25_000_000 or min(im.size) < 128:
            raise ValueError()
        im.load()
        return ImageOps.exif_transpose(im).convert("RGB")
    except Exception as exc:
        raise ValueError("Choose a non-animated JPG, PNG or WebP between 128 pixels and 25 megapixels.") from exc


def png(im):
    output = io.BytesIO()
    im.save(output, format="PNG")
    return output.getvalue()


def create(db, name, data, crop):
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("Enter a product name (up to 120 characters).")
    im = decode_image(data)
    x, y, w, h = crop
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1-x+1e-6 and 0 < h <= 1-y+1e-6):
        raise ValueError("Select a valid product area.")
    cropped = im.crop((round(x*im.width), round(y*im.height), round((x+w)*im.width), round((y+h)*im.height)))
    if min(cropped.size) < 128:
        raise ValueError("Select a larger product area (at least 128 pixels each side).")
    ident = str(uuid.uuid4())
    keys = [f"products/{ident}/original.png", f"products/{ident}/crop.png"]
    uploaded = []
    try:
        for key, image in zip(keys, (im, cropped)):
            storage_service.upload_bytes(key, png(image), content_type="image/png")
            uploaded.append(key)
        row = Product(id=ident, name=name, original_key=keys[0], crop_key=keys[1])
        db.add(row)
        db.commit()
        return row
    except Exception:
        db.rollback()
        for key in uploaded:
            storage_service.delete_object(key)
        raise


def fal_request(path, payload=None):
    if not settings.fal_api_key:
        raise ValueError("Product preparation is not configured. You can use your cropped original instead.")
    with httpx.Client(timeout=45) as client:
        response = client.request("POST" if payload is not None else "GET",
            "https://queue.fal.run/pixelcut/product-photo" + path,
            headers={"Authorization": "Key " + settings.fal_api_key},
            **({"json": payload} if payload is not None else {}))
        response.raise_for_status()
        return response.json()


def prepare(db, row):
    # Claim before any paid submission: repeated clicks never submit twice.
    claimed = db.query(Product).filter(Product.id == row.id, Product.status == "uploaded").update(
        {"status": "submitting", "error": None, "started_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()
    db.refresh(row)
    if not claimed:
        return row
    try:
        result = fal_request("", {"image_url": storage_service.asset_url(row.crop_key),
            "image_size": "square_hd", "background": {"mode": "Color", "color": {"r":255,"g":255,"b":255}},
            "margin": {"all":"10%"}, "output_format":"png", "sync_mode":True})
        row.request_id = result["request_id"]
        row.status = "processing"
    except Exception:
        # An uncertain paid submission must not be silently retried.
        row.status = "failed"
        row.error = "Preparation could not be confirmed. Your original is safe. Use the cropped original or upload again; another preparation may incur a charge."
    db.commit()
    return row


def refresh(db, row):
    if row.status == "submitting" and row.started_at and (datetime.utcnow()-row.started_at).total_seconds() > 300:
        row.status = "failed"
        row.error = "Preparation took too long. Your original is safe. Use the cropped original; no automatic paid retry was started."
        db.commit()
    if row.status != "processing" or not row.request_id:
        return row
    try:
        path = "/requests/" + row.request_id
        status = fal_request(path + "/status")
        if status["status"] != "COMPLETED":
            if row.started_at and (datetime.utcnow()-row.started_at).total_seconds() > 300:
                row.status = "failed"
                row.error = "Preparation took too long. Your original is safe; use the cropped original. No automatic paid retry was started."
                db.commit()
            return row
        result = fal_request(path)
        url = result["image"]["url"]
        if not url.startswith("data:image/"):
            raise ValueError("Expected inline image result")
        data = base64.b64decode(url.split(",",1)[1], validate=True)
        image = decode_image(data)
        key = f"products/{row.id}/prepared.png"
        storage_service.upload_bytes(key, png(image), content_type="image/png")
        # Approval may have happened concurrently; do not revert it.
        db.query(Product).filter(Product.id == row.id, Product.status == "processing").update(
            {"prepared_key": key, "status":"review", "error":None}, synchronize_session=False)
        db.commit()
        db.refresh(row)
    except httpx.RequestError:
        return row  # temporary poll failure: existing task remains retrievable
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429 or exc.response.status_code >= 500:
            return row
        db.query(Product).filter(Product.id == row.id, Product.status == "processing").update(
            {"status":"failed", "error":"Preparation failed. Your original is safe; use the cropped original or upload a different photo."}, synchronize_session=False)
        db.commit()
        db.refresh(row)
    except Exception:
        db.query(Product).filter(Product.id == row.id, Product.status == "processing").update(
            {"status":"failed", "error":"We couldn't prepare this image. Your original is safe; use the cropped original or upload a different photo."}, synchronize_session=False)
        db.commit()
        db.refresh(row)
    return row


def approve(db, row, version):
    key = row.prepared_key if version == "prepared" else row.crop_key
    if version not in {"prepared", "original"} or not key:
        raise ValueError("That product image is not ready to approve.")
    if row.status in {"processing", "submitting"}:
        raise ValueError("Wait for preparation to finish before choosing an image.")
    if row.status == "approved":
        return row  # approved products are immutable
    changed = db.query(Product).filter(Product.id == row.id, Product.status.in_(["uploaded", "review", "failed"])).update(
        {"accepted_key":key, "status":"approved"}, synchronize_session=False)
    db.commit()
    db.refresh(row)
    if not changed and row.status != "approved":
        raise ValueError("This product changed. Refresh it before choosing an image.")
    return row


def public(row):
    return {"id":row.id,"name":row.name,"status":row.status,"error":row.error,
        **{field: storage_service.asset_url(getattr(row, key)) if getattr(row,key) else None
           for field,key in [("original_url","original_key"),("crop_url","crop_key"),
                             ("prepared_url","prepared_key"),("accepted_url","accepted_key")]}}


def selected(db, ids):
    rows = [db.get(Product, ident) for ident in dict.fromkeys(ids)]
    if any(row is None or row.status != "approved" or not row.accepted_key for row in rows):
        raise ValueError("Approve each selected product image before creating your video.")
    return rows


def job_references(db, job_id):
    return [{"product_id":p.product_id,"name":p.name,"object_key":p.object_key,"views":json.loads(p.views_json or "[]")}
            for p in db.query(JobProduct).filter_by(job_id=job_id).all()]
