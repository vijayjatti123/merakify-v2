import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import CharacterGenerate, CharacterOut, CharacterVoice
from app.services import character_image_service, character_service, storage_service

router = APIRouter(prefix="/api/characters", tags=["characters"])


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty")
    return normalized


def _replaceable_character(db: Session, character_id: str | None):
    if not character_id:
        return None
    try:
        return character_service.require_replaceable_draft(db, character_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except character_service.CharacterStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _generated_image_extension(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(content_type.lower(), ".png")


@router.post("/generate", response_model=CharacterOut)
def generate_character(payload: CharacterGenerate, db: Session = Depends(get_db)):
    name = _required_text(payload.name, "name")
    description = _required_text(payload.description, "description")
    character = _replaceable_character(db, payload.character_id)

    try:
        image = character_image_service.generate_character_image(name, description)
    except character_image_service.CharacterImageGenerationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    object_key = f"characters/{uuid.uuid4()}{_generated_image_extension(image.content_type)}"
    uploaded = storage_service.upload_bytes(
        object_key,
        image.data,
        content_type=image.content_type,
        cache_control="private, max-age=3600",
    )
    try:
        return character_service.save_draft_image(
            db,
            character=character,
            name=name,
            description=description,
            image_url=uploaded["url"],
            image_source="generated",
        )
    except Exception:
        storage_service.delete_object(uploaded["key"])
        raise


@router.post("/upload", response_model=CharacterOut)
def upload_character(
    name: str = Form(...),
    description: str = Form(...),
    file: UploadFile = File(...),
    character_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    normalized_name = _required_text(name, "name")
    normalized_description = _required_text(description, "description")
    character = _replaceable_character(db, character_id)
    if not file.content_type or not file.content_type.lower().startswith("image/"):
        raise HTTPException(status_code=400, detail="file must be an image")

    filename = Path(file.filename or "reference-image").name.strip() or "reference-image"
    object_key = f"characters/{uuid.uuid4()}/{filename}"
    uploaded = storage_service.upload_file(
        object_key,
        file.file,
        content_type=file.content_type,
        cache_control="private, max-age=3600",
    )
    try:
        return character_service.save_draft_image(
            db,
            character=character,
            name=normalized_name,
            description=normalized_description,
            image_url=uploaded["url"],
            image_source="uploaded",
        )
    except Exception:
        storage_service.delete_object(uploaded["key"])
        raise


@router.post("/{character_id}/voice", response_model=CharacterOut)
def set_character_voice(
    character_id: str,
    payload: CharacterVoice,
    db: Session = Depends(get_db),
):
    character = character_service.get_character(db, character_id)
    if not character:
        raise HTTPException(status_code=404, detail="character not found")
    try:
        return character_service.set_voice(db, character, payload.voice_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{character_id}/approve", response_model=CharacterOut)
def approve_character(character_id: str, db: Session = Depends(get_db)):
    character = character_service.get_character(db, character_id)
    if not character:
        raise HTTPException(status_code=404, detail="character not found")
    try:
        return character_service.approve_character(db, character)
    except character_service.CharacterStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("", response_model=list[CharacterOut])
def list_characters(db: Session = Depends(get_db)):
    return character_service.list_approved_characters(db)
