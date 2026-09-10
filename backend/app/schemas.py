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


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str
    aspect_ratio: Literal["9:16", "16:9"] = "16:9"
    quality: Literal["480p", "720p"] = "720p"
    language: str = "English"
    ai_model: Literal["Wan 2.5", "Seedance 2.0", "Kling 3.0", "Seedance 2.5", "Veo 3.1", "Sora 2"] = (
        "Seedance 2.5"
    )

    @model_validator(mode="after")
    def validate_language_model_compatibility(self):
        if self.language.strip().lower() != "english" and self.ai_model not in {"Seedance 2.0", "Seedance 2.5"}:
            raise ValueError("non-English jobs require Seedance 2.0 or Seedance 2.5")
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
