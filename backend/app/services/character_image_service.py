import base64
import json
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.config import settings
from app.agents import prompts


class CharacterImageGenerationError(RuntimeError):
    """Raised when Google does not return a usable generated image."""


@dataclass(frozen=True)
class GeneratedCharacterImage:
    data: bytes
    content_type: str


def generate_character_image(name: str, description: str) -> GeneratedCharacterImage:
    """Generate one character reference image through Google AI Studio directly."""
    api_key = settings.google_ai_api_key.strip()
    if not api_key:
        raise CharacterImageGenerationError("GOOGLE_AI_API_KEY is required for character image generation")

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


def generate_character_reference_sheet(
    name: str,
    description: str,
    reference_image_url: str,
    *,
    visual_style: str | None = None,
    qa_feedback: str = "",
) -> GeneratedCharacterImage:
    """Generate a turnaround sheet conditioned on the character's approved image."""
    api_key = settings.google_ai_api_key.strip()
    if not api_key:
        raise CharacterImageGenerationError("GOOGLE_AI_API_KEY is required for reference-sheet generation")

    model = settings.gemini_image_model.strip()
    if not model:
        raise CharacterImageGenerationError("GEMINI_IMAGE_MODEL is required for reference-sheet generation")

    reference = _download_reference_image(reference_image_url)
    reference_data, reference_content_type = reference.data, reference.content_type

    prompt = (
        "Using the supplied character image as the strict visual reference, create one clean composite "
        "character turnaround sheet on a neutral studio background. Include exactly three full-body views: "
        "a front T-pose, a side profile, and a back view. Also include a tidy grid of 10 to 15 head-only "
        "close-ups at varied camera angles, plus one larger hero portrait. Preserve the exact same identity, "
        "face, facial proportions, skin tone, hair, outfit, colors, accessories, and distinguishing details "
        "from the reference image in every panel. Do not redesign, age, restyle, or change the character. "
        f"Character name: {name}. Existing description: {description}. No captions, labels, or typography."
    )
    if visual_style is not None:
        prompt = (
            prompts.CHARACTER_STYLE_VARIANT
            + f"\nRequested rendering: {visual_style}. Character name: {name}. Description: {description}."
        )
        if qa_feedback:
            prompt += f"\nCorrect the previous candidate's visual QA issue: {qa_feedback}"
    payload = json.dumps(
        {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": reference_content_type,
                                "data": base64.b64encode(reference_data).decode("ascii"),
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "3:4" if visual_style else "16:9"},
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
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )

    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise CharacterImageGenerationError(
            f"Google reference-sheet generation failed with HTTP {error.code}: {detail}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise CharacterImageGenerationError(
            f"Google reference-sheet generation could not be reached: {error}"
        ) from error

    for candidate in result.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline_data = part.get("inlineData")
            if inline_data and inline_data.get("data"):
                try:
                    image = base64.b64decode(inline_data["data"], validate=True)
                except (ValueError, TypeError) as error:
                    raise CharacterImageGenerationError(
                        "Google returned invalid base64 reference-sheet data"
                    ) from error
                if image:
                    return GeneratedCharacterImage(
                        data=image,
                        content_type=inline_data.get("mimeType") or "image/png",
                    )

    raise CharacterImageGenerationError("Google returned no reference-sheet image")


def _download_reference_image(url: str) -> GeneratedCharacterImage:
    try:
        with urlopen(Request(url, headers={"User-Agent": "Merakify/1.0"}), timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
            content_type = response.headers.get_content_type()
    except (HTTPError, URLError, TimeoutError) as error:
        # URLs may carry signatures: do not copy provider/download error details into traces.
        raise CharacterImageGenerationError("Reference image download failed") from error
    if not data or len(data) > 20 * 1024 * 1024 or not content_type.startswith("image/"):
        raise CharacterImageGenerationError("Reference image must be a non-empty image under 20 MB")
    return GeneratedCharacterImage(data, content_type)


def check_character_style_variant(
    reference_image_url: str, image: GeneratedCharacterImage, visual_style: str,
) -> dict:
    """Compare actual source/candidate pixels before a variant may enter the cache."""
    reference = _download_reference_image(reference_image_url)
    payload = {
        "contents": [{"parts": [
            {"text": prompts.CHARACTER_STYLE_QA + f"\nRequested rendering: {visual_style}. Image 1 is the identity reference; image 2 is the candidate."},
            {"inlineData": {"mimeType": reference.content_type, "data": base64.b64encode(reference.data).decode("ascii")}},
            {"inlineData": {"mimeType": image.content_type, "data": base64.b64encode(image.data).decode("ascii")}},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT"]},
    }
    request = Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(settings.gemini_image_model.strip(), safe='')}:generateContent",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": settings.google_ai_api_key.strip()},
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
        text = "".join(
            part.get("text", "") for candidate in result.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", []) if not part.get("thought")
        )
        verdict = json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        if not isinstance(verdict.get("approved"), bool) or not isinstance(verdict.get("reason"), str):
            raise ValueError("Invalid visual QA verdict")
        return verdict
    except Exception as error:
        raise CharacterImageGenerationError("Character style visual QA failed") from error
