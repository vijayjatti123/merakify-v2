import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=new_id)
    brief = Column(Text, nullable=False)
    aspect_ratio = Column(String, nullable=False, default="16:9")
    quality = Column(String, nullable=False, default="720p")
    language = Column(String, nullable=False, default="English")
    ai_model = Column(String, nullable=False, default="Seedance 2.5")
    status = Column(String, default="queued")  # queued | running | done | error
    error_message = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)  # full pipeline output, stored as JSON text
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events = relationship("AgentEvent", back_populates="job", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"

    id = Column(String, primary_key=True, default=new_id)
    filename = Column(String, nullable=False)
    object_key = Column(String, nullable=False, unique=True)
    url = Column(Text, nullable=False)
    role = Column(String, nullable=True)
    label = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Character(Base):
    __tablename__ = "characters"

    id = Column(String, primary_key=True, default=new_id)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    image_url = Column(Text, nullable=False)
    image_source = Column(String, nullable=False)  # generated | uploaded
    voice_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="draft")  # draft | approved
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id = Column(String, primary_key=True, default=new_id)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False)
    agent_key = Column(String, nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="events")
