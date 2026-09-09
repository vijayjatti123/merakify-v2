from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class JobCreate(BaseModel):
    brief: str


class ShotEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_number: int
    dialogue_text: str
    description: str


class JobRevise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[ShotEdit]


class JobOut(BaseModel):
    id: str
    brief: str
    status: str
    error_message: Optional[str] = None
    result: Optional[Any] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AgentEventOut(BaseModel):
    agent_key: str
    note: str
    created_at: datetime

    class Config:
        from_attributes = True
