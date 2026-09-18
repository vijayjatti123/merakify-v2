"""Display-only derivatives. Original accepted bytes remain generation/QA inputs."""
import io
import json
import time

from PIL import Image, ImageOps
from app.services import storage_service


def store_variants(image, original_key, *, emit, shot_number):
    started = time.monotonic()
    with Image.open(io.BytesIO(image.data)) as source:
        source.load()
        icc_profile = source.info.get("icc_profile", b"")
        alpha = "A" in source.getbands() or "transparency" in source.info
        decoded = ImageOps.exif_transpose(source).convert("RGBA" if alpha else "RGB")
    emit("preview_timing", json.dumps({"shot_number": shot_number, "phase": "display_decode",
        "elapsed_sec": round(time.monotonic() - started, 3)}))
    variants = []
    for width in sorted({min(480, decoded.width), min(960, decoded.width)}):
        started = time.monotonic()
        thumbnail = decoded.copy()
        thumbnail.thumbnail((width, max(1, round(decoded.height * width / decoded.width))), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        # Lossless WebP avoids a second lossy compression of product lettering.
        # Spatial resizing applies ONLY to display; never overwrite the source.
        thumbnail.save(out, format="WEBP", lossless=True, method=3, icc_profile=icc_profile)
        data = out.getvalue()
        emit("preview_timing", json.dumps({"shot_number": shot_number, "phase": "display_encode",
            "width": thumbnail.width, "bytes": len(data), "elapsed_sec": round(time.monotonic() - started, 3)}))
        if len(data) >= len(image.data):
            continue  # WebP is not inherently smaller than an existing JPEG.
        started = time.monotonic()
        stored = storage_service.upload_bytes(key=f"{original_key}.display-{thumbnail.width}.webp",
            body=data, content_type="image/webp", cache_control="private, max-age=300")
        variants.append({"key": stored["key"], "url": stored["url"], "width": thumbnail.width,
                         "height": thumbnail.height, "bytes": len(data)})
        emit("preview_timing", json.dumps({"shot_number": shot_number, "phase": "display_upload",
            "width": thumbnail.width, "elapsed_sec": round(time.monotonic() - started, 3)}))
    return {"source_key": original_key, "width": decoded.width, "height": decoded.height, "variants": variants}
