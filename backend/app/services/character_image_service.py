import base64
import json
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.config import settings


class CharacterImageGenerationError(RuntimeError):
    """Raised when Google does not return a usable generated image."""


@dataclass(frozen=True)
class GeneratedCharacterImage:
    data: bytes
    content_type: str


def generate_character_image(name: str, description: str) -> GeneratedCharacterImage:
    """Generate one character reference image through Google AI Studio directly."""
    api_key = settings.gemini_api_key.strip()
    if not api_key:
        raise CharacterImageGenerationError("GEMINI_API_KEY is required for character image generation")

    model = settings.gemini_image_model.strip()
    if not model:
        raise CharacterImageGenerationError("GEMINI_IMAGE_MODEL is required for character image generation")

    variation_id = uuid.uuid4().hex
    prompt = (
        "Create a single cinematic character reference portrait for film-production continuity. "
        f"Character name: {name}. Character description: {description}. "
        "Show one person only, full body or three-quarter view, neutral uncluttered studio background, "
        "clear face, clothing, hair, and distinguishing details, natural realistic lighting, no text, "
        f"no typography, no collage. Creative variation token: {variation_id}."
    )
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "3:4"},
            },
        }
    ).encode("utf-8")
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent"
    )
    request = Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )

    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise CharacterImageGenerationError(
            f"Google image generation failed with HTTP {error.code}: {detail}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise CharacterImageGenerationError(f"Google image generation could not be reached: {error}") from error

    for candidate in result.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline_data = part.get("inlineData")
            if inline_data and inline_data.get("data"):
                try:
                    image = base64.b64decode(inline_data["data"], validate=True)
                except (ValueError, TypeError) as error:
                    raise CharacterImageGenerationError("Google returned invalid base64 image data") from error
                if not image:
                    break
                return GeneratedCharacterImage(
                    data=image,
                    content_type=inline_data.get("mimeType") or "image/png",
                )

    raise CharacterImageGenerationError("Google returned no generated image")
