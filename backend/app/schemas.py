from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class JobCreate(BaseModel):
    brief: str


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
