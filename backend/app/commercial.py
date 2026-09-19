"""Typed commercial intake shared by clarification and the existing Director."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

AdType = Literal["character", "product", "cgi", "ugc"]


class CommercialBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    audience: str = Field(default="", max_length=500)
    selling_point: str = Field(default="", max_length=1000)
    call_to_action: str = Field(default="", max_length=500)
    treatment: str = Field(default="", max_length=1500)
    must_preserve: str = Field(default="", max_length=1000)
    audio_mode: Literal["auto", "silent", "voiceover", "onscreen"] = "auto"


def normalized_brief(value=None):
    return CommercialBrief.model_validate(value or {}).model_dump(exclude_defaults=True)


def validate_commercial(ad_type, brief, product_ids):
    if ad_type not in ("character", "product", "cgi", "ugc"):
        raise ValueError("Choose a supported commercial type")
    if ad_type in ("product", "cgi") and not product_ids:
        raise ValueError("Add an approved product reference before creating this commercial")
    return normalized_brief(brief)


# Opening-frame requirements only. These feed the existing generation AND checker
# inputs, and their fingerprint. Never leak treatment's future action into a still.
OPENING_REQUIREMENTS = {
    "product": "For products actually visible in this opening: preserve approved package geometry, label, color, scale and surface contact. Show the planned lighting/reflections without altering the label. Do not insert a product merely because a reference is attached.",
    "cgi": "Freeze only the planned beginning of the effect. Preserve the visible product's approved geometry, label and material unless the opening explicitly specifies a change. Keep initial support, scale and occlusion readable. Do not show the transformation's ending early.",
    "ugc": "Follow the planned phone framing, believable room lighting, eyeline and hand contact. Preserve the visible creator's identity and any visible approved product. Do not invent captions, testimonial text or a customer endorsement.",
}
