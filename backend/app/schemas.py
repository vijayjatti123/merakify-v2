from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


AssetRole = Literal["background", "location", "prop", "product", "other"]


class ScriptExtract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_text: str = Field(min_length=1)


class ScriptExtractionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters: list[str]
    locations: list[str]


class CharacterResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["vault", "invent"]
    character_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_character_id(self):
        if self.mode == "vault" and not self.character_id:
            raise ValueError("vault character resolutions require character_id")
        if self.mode == "invent" and self.character_id is not None:
            raise ValueError("invent character resolutions cannot include character_id")
        return self


class LocationResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["asset", "invent"]
    asset_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_asset_id(self):
        if self.mode == "asset" and not self.asset_id:
            raise ValueError("asset location resolutions require asset_id")
        if self.mode == "invent" and self.asset_id is not None:
            raise ValueError("invent location resolutions cannot include asset_id")
        return self


class ScriptResolutionMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters: dict[str, CharacterResolution] = Field(default_factory=dict)
    locations: dict[str, LocationResolution] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_names(self):
        if any(not name.strip() for name in [*self.characters, *self.locations]):
            raise ValueError("resolution names cannot be empty")
        return self


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str
    aspect_ratio: Literal["9:16", "16:9"] = "16:9"
    quality: Literal["480p", "720p"] = "720p"
    language: str = "English"
    ai_model: Literal["Wan 2.5", "Seedance 2.0", "Kling 3.0", "Seedance 2.5", "Veo 3.1", "Sora 2"] = (
        "Seedance 2.5"
    )
    script_text: Optional[str] = None
    resolutions: Optional[ScriptResolutionMap] = None

    @model_validator(mode="after")
    def validate_request(self):
        if (self.script_text is None) != (self.resolutions is None):
            raise ValueError("script_text and resolutions must be provided together")
        if self.script_text is not None and not self.script_text.strip():
            raise ValueError("script_text cannot be empty")
        return self


class ShotEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_number: int
    dialogue_text: str
    description: str


class JobRevise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[ShotEdit]


class JobRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_request: Optional[str] = None


class ShotRegenerateHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    angle: Optional[Literal["lowangle", "highangle", "overhead", "pov", "overtheshoulder", "eyelevel", "dutch"]] = None
    movement: Optional[Literal["dollyin", "dollyout", "tracking", "orbit", "handheld", "craneup", "cranedown", "static"]] = None
    lighting: Optional[Literal["goldenhour", "bluehour", "moody", "softlight", "rimlight", "silhouette", "spotlight", "backlight"]] = None
    composition: Optional[Literal["leadinglines", "ruleofthirds", "symmetry", "negativespace", "foreground"]] = None


class JobOut(BaseModel):
    id: str
    brief: str
    aspect_ratio: str
    quality: str
    language: str
    ai_model: str
    status: str
    error_message: Optional[str] = None
    result: Optional[Any] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssetOut(BaseModel):
    id: str
    filename: str
    url: str
    role: Optional[AssetRole] = None
    label: Optional[str] = None
    created_at: datetime


class CharacterGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    character_id: Optional[str] = None


class CharacterVoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str


class CharacterOut(BaseModel):
    id: str
    name: str
    description: str
    image_url: str
    reference_sheet_url: Optional[str] = None
    reference_sheet_error: Optional[str] = None
    image_source: Literal["generated", "uploaded"]
    voice_id: Optional[str] = None
    status: Literal["draft", "approved"]
    created_at: datetime

    class Config:
        from_attributes = True


class AgentEventOut(BaseModel):
    agent_key: str
    note: str
    created_at: datetime

    class Config:
        from_attributes = True
