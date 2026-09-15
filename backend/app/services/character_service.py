from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Character


CHARACTER_STATUS_DRAFT = "draft"
CHARACTER_STATUS_APPROVED = "approved"

# Sarvam Bulbul v3 speaker names are fixed catalog identifiers, not cloned voices.
SARVAM_VOICE_IDS = (
    "shubh",
    "aditya",
    "rahul",
    "rohan",
    "amit",
    "dev",
    "ratan",
    "varun",
    "manan",
    "sumit",
    "kabir",
    "aayan",
    "ashutosh",
    "advait",
    "anand",
    "tarun",
    "sunny",
    "mani",
    "gokul",
    "vijay",
    "mohit",
    "rehan",
    "soham",
    "ritu",
    "priya",
    "neha",
    "pooja",
    "simran",
    "kavya",
    "ishita",
    "shreya",
    "roopa",
    "tanya",
    "shruti",
    "suhani",
    "kavitha",
    "rupali",
)


class CharacterStateError(ValueError):
    """Raised when a requested vault transition is not allowed."""


def get_character(db: Session, character_id: str) -> Character | None:
    return db.query(Character).filter(Character.id == character_id).first()


def require_replaceable_draft(db: Session, character_id: str) -> Character:
    character = get_character(db, character_id)
    if not character:
        raise LookupError("character not found")
    if character.status != CHARACTER_STATUS_DRAFT:
        raise CharacterStateError("approved characters cannot have their reference image replaced")
    return character


def save_draft_image(
    db: Session,
    *,
    name: str,
    description: str,
    image_url: str,
    image_source: str,
    character: Character | None = None,
    display_name: str | None = None,
    catalog_status: str = "review_required",
) -> Character:
    if image_source not in {"generated", "uploaded"}:
        raise ValueError("invalid character image source")
    normalized_display = validate_presentation(display_name, catalog_status)

    if character is None:
        character = Character(
            name=name,
            description=description,
            image_url=image_url,
            image_source=image_source,
            status=CHARACTER_STATUS_DRAFT,
            display_name=normalized_display,
            catalog_status=catalog_status,
        )
        db.add(character)
    else:
        character.name = name
        character.description = description
        character.image_url = image_url
        character.image_source = image_source
        character.display_name = normalized_display
        character.catalog_status = catalog_status

    db.commit()
    db.refresh(character)
    return character


def set_voice(db: Session, character: Character, voice_id: str) -> Character:
    normalized_voice = voice_id.strip().lower()
    if normalized_voice not in SARVAM_VOICE_IDS:
        raise ValueError("voice_id is not in the Sarvam Bulbul v3 catalog")
    character.voice_id = normalized_voice
    db.commit()
    db.refresh(character)
    return character


def approve_character(db: Session, character: Character) -> Character:
    if not character.voice_id:
        raise CharacterStateError("choose a Sarvam voice before approving the character")
    character.status = CHARACTER_STATUS_APPROVED
    db.commit()
    db.refresh(character)
    return character


def save_reference_sheet(db: Session, character: Character, reference_sheet_url: str) -> Character:
    character.reference_sheet_url = reference_sheet_url
    db.commit()
    db.refresh(character)
    return character


def list_approved_characters(db: Session) -> list[Character]:
    return (
        db.query(Character)
        .filter(Character.status == CHARACTER_STATUS_APPROVED)
        .order_by(Character.created_at.desc())
        .all()
    )


def validate_presentation(display_name: str | None, catalog_status: str) -> str | None:
    if catalog_status not in {"customer", "test", "review_required"}:
        raise ValueError("invalid character catalog status")
    name = display_name.strip() if display_name else None
    if name and (len(name) > 80 or any(ord(c) < 32 for c in name)):
        raise ValueError("display name must be at most 80 characters without control characters")
    if catalog_status == "customer" and not name:
        raise ValueError("customer characters require a display name")
    return name


def set_presentation(db: Session, character: Character, display_name: str, catalog_status: str) -> Character:
    character.display_name = validate_presentation(display_name, catalog_status)
    character.catalog_status = catalog_status
    db.commit()
    db.refresh(character)
    return character


def is_customer_selectable(character: Character) -> bool:
    return character.status == CHARACTER_STATUS_APPROVED and character.catalog_status == "customer" and bool((character.display_name or "").strip())


def list_customer_characters(db: Session) -> list[Character]:
    # Keep the internal approved lookup intact for previously bound jobs.
    return db.query(Character).filter(
        Character.status == CHARACTER_STATUS_APPROVED,
        Character.catalog_status == "customer",
        Character.display_name.isnot(None),
        func.trim(Character.display_name) != "",
    ).order_by(Character.created_at.desc()).all()
