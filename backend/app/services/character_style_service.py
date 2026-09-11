"""Automatic, cached visual renderings of an immutable approved vault identity."""
import uuid
from urllib.parse import unquote, urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Character, CharacterStyleVariant
from app.services import character_image_service, storage_service


# Exact values used by NewJob's existing Visual Style dropdown.
VISUAL_STYLES = (
    "Natural", "Cinematic", "Realistic", "Cartoon / Anime", "3D / CGI",
    "Hyper-realistic", "Vintage / retro film",
)


def visual_style_from_brief(brief: str) -> str:
    # Module F appends a standalone line; the last exact toolbar line wins.
    for line in reversed(brief.splitlines()):
        for style in VISUAL_STYLES:
            if line.strip() == f"Visual style: {style}.":
                return style
    return "Natural"


def _fresh_url(url: str) -> str:
    """Renew access to our existing S3 object without changing its identity."""
    parsed = urlsplit(url)
    owned_hosts = {
        f"{settings.aws_s3_bucket}.s3.{settings.aws_region}.amazonaws.com",
        f"{settings.aws_s3_bucket}.s3.amazonaws.com",
    }
    if settings.aws_s3_bucket and parsed.scheme == "https" and parsed.hostname in owned_hosts:
        return storage_service.asset_url(unquote(parsed.path.lstrip("/")))
    return url


def _get_variant(db: Session, character_id: str, visual_style: str):
    return db.query(CharacterStyleVariant).filter_by(
        character_id=character_id, visual_style=visual_style,
    ).first()


def _variant_fields(variant: CharacterStyleVariant) -> dict:
    return {
        "image_url": variant.image_url,
        "character_id": variant.character_id,
        "visual_style": variant.visual_style,
        "style_variant_id": variant.id,
        "style_variant_source": variant.source,
    }


def resolve_character_rendering(db: Session, character: Character, visual_style: str, emit=None) -> dict:
    if visual_style == "Natural":
        return {"image_url": character.image_url}
    if visual_style not in VISUAL_STYLES:
        raise ValueError("Unknown visual style")
    notify = emit or (lambda *_args: None)
    uploaded_key = None
    try:
        # An independent transaction prevents variant rollback from discarding job events.
        # PostgreSQL serializes concurrent generation for this identity. SQLite still
        # enforces the unique pair; a competing insert reuses the winner below.
        with Session(bind=db.get_bind()) as variants_db:
            locked = variants_db.query(Character).filter_by(id=character.id).with_for_update().one()
            cached = _get_variant(variants_db, character.id, visual_style)
            if cached:
                cached.image_url = _fresh_url(cached.image_url)
                fields = _variant_fields(cached)
                variants_db.commit()
                notify("continuity_plan", f'Reused cached {visual_style} variant for "{character.name}".')
                return fields

            reference_url = _fresh_url(locked.image_url)
            feedback = ""
            for attempt in range(2):
                notify("continuity_plan", f'Generating {visual_style} variant for "{character.name}" (attempt {attempt + 1}/2).')
                image = character_image_service.generate_character_reference_sheet(
                    locked.name, locked.description, reference_url,
                    visual_style=visual_style, qa_feedback=feedback,
                )
                verdict = character_image_service.check_character_style_variant(reference_url, image, visual_style)
                if verdict["approved"]:
                    break
                feedback = verdict["reason"]
                notify("continuity_plan", f'Visual identity/style QA rejected the variant for "{character.name}".'
                       + (" Retrying automatically." if attempt == 0 else " Using the base reference."))
            else:
                raise character_image_service.CharacterImageGenerationError("Style variant did not pass visual QA")

            extension = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(image.content_type, ".png")
            uploaded = storage_service.upload_bytes(
                f"characters/{character.id}/style-variants/{uuid.uuid4()}{extension}", image.data,
                content_type=image.content_type, cache_control="private, max-age=3600",
            )
            uploaded_key = uploaded["key"]
            variant = CharacterStyleVariant(
                character_id=character.id, visual_style=visual_style, image_url=uploaded["url"],
                source="style_transfer_from_upload" if locked.image_source == "uploaded" else "generated_variant",
            )
            variants_db.add(variant)
            try:
                variants_db.commit()
            except IntegrityError:
                variants_db.rollback()
                winner = _get_variant(variants_db, character.id, visual_style)
                if winner is None:
                    raise
                storage_service.delete_object(uploaded_key)
                uploaded_key = None
                winner.image_url = _fresh_url(winner.image_url)
                fields = _variant_fields(winner)
                variants_db.commit()
                notify("continuity_plan", f'Reused concurrently cached {visual_style} variant for "{character.name}".')
                return fields
            fields = _variant_fields(variant)
            uploaded_key = None  # The committed cache now owns this object.
            notify("continuity_plan", f'Cached QA-approved {visual_style} variant for "{character.name}".')
            return fields
    except Exception as error:
        if uploaded_key:
            try:
                storage_service.delete_object(uploaded_key)
            except Exception:
                notify("continuity_plan", "Warning: could not remove an unpersisted style-variant image.")
        # Provider exception bodies may contain signed URLs or other sensitive data.
        warning = f'{visual_style} variant unavailable for "{character.name}" ({type(error).__name__}); using the Natural base image. Visual style mismatch fallback.'
        notify("continuity_plan", f"Warning: {warning}")
        return {"image_url": character.image_url, "style_variant_warning": warning}
