from sqlalchemy.orm import Session

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
) -> Character:
    if image_source not in {"generated", "uploaded"}:
        raise ValueError("invalid character image source")

    if character is None:
        character = Character(
            name=name,
            description=description,
            image_url=image_url,
            image_source=image_source,
            status=CHARACTER_STATUS_DRAFT,
        )
        db.add(character)
    else:
        character.name = name
        character.description = description
        character.image_url = image_url
        character.image_source = image_source

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


def list_approved_characters(db: Session) -> list[Character]:
    return (
        db.query(Character)
        .filter(Character.status == CHARACTER_STATUS_APPROVED)
        .order_by(Character.created_at.desc())
        .all()
    )
