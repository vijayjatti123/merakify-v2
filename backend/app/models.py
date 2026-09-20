import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, Float, Integer, DateTime, ForeignKey, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


class ClarifierSession(Base):
    """Independent intake record; a nullable user_id can be added with future auth."""
    __tablename__ = "clarifier_sessions"
    session_id = Column(String, primary_key=True, default=new_id)
    raw_brief = Column(Text, nullable=False)
    known_fields = Column(JSON, nullable=False, default=dict)
    turns = Column(JSON, nullable=False, default=list)
    gathered = Column(JSON, nullable=False, default=dict)
    confidence = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="active")
    refined_prompt = Column(Text, nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=new_id)
    brief = Column(Text, nullable=False)
    ad_type = Column(String, nullable=False, default="character")
    ad_brief_json = Column(Text, nullable=True)
    aspect_ratio = Column(String, nullable=False, default="16:9")
    visual_style = Column(String, nullable=False, default="Natural")
    color_grade = Column(String, nullable=False, default="None")
    quality = Column(String, nullable=False, default="720p")
    language = Column(String, nullable=False, default="English")
    ai_model = Column(String, nullable=False, default="Seedance 2.5")
    video_model = Column(String, nullable=True)
    script_text = Column(Text, nullable=True)
    resolutions_json = Column(Text, nullable=True)
    creative_direction_json = Column(Text, nullable=True)
    status = Column(String, default="queued")  # queued | running | done | error
    error_message = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)  # full pipeline output, stored as JSON text
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events = relationship("AgentEvent", back_populates="job", cascade="all, delete-orphan")


class PipelineTask(Base):
    """Durable dispatch/lease record; executes existing Director entry points."""
    __tablename__ = "pipeline_tasks"
    job_id = Column(String, ForeignKey("jobs.id"), primary_key=True)
    kind = Column(String, nullable=False)
    token = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued")
    heartbeat_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AgentCheckpoint(Base):
    """Private completed text responses, keyed by exact versioned inputs."""
    __tablename__ = "agent_checkpoints"
    job_id = Column(String, ForeignKey("jobs.id"), primary_key=True)
    input_hash = Column(String, primary_key=True)
    response_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class VideoTask(Base):
    """A durable, single paid attempt per shot; independent of mutable planning JSON."""
    __tablename__ = "video_tasks"
    __table_args__ = (UniqueConstraint("job_id", "shot_number", name="uq_video_job_shot"),)
    id = Column(String, primary_key=True, default=new_id)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False, index=True)
    shot_number = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    data_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    display_name = Column(String(80), nullable=True)
    catalog_status = Column(String, nullable=False, default="review_required", server_default="review_required")
    description = Column(Text, nullable=False)
    image_url = Column(Text, nullable=False)
    reference_sheet_url = Column(Text, nullable=True)
    image_source = Column(String, nullable=False)  # generated | uploaded
    voice_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="draft")  # draft | approved
    created_at = Column(DateTime, default=datetime.utcnow)


class CharacterStyleVariant(Base):
    __tablename__ = "character_style_variants"
    __table_args__ = (UniqueConstraint("character_id", "visual_style", name="uq_character_visual_style"),)

    id = Column(String, primary_key=True, default=new_id)
    character_id = Column(String, ForeignKey("characters.id"), nullable=False)
    visual_style = Column(String, nullable=False)
    image_url = Column(Text, nullable=False)
    source = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class VoiceRateProfile(Base):
    __tablename__ = "voice_rate_profiles"

    voice_id = Column(String, primary_key=True)
    language = Column(String, primary_key=True)
    chars_per_second = Column(Float, nullable=False)
    measured_at = Column(DateTime, nullable=False)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id = Column(String, primary_key=True, default=new_id)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False)
    agent_key = Column(String, nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="events")


class FinalAssembly(Base):
    """Explicit job-level stitching attempt, independent of mutable planning results."""
    __tablename__ = "final_assemblies"
    job_id = Column(String, ForeignKey("jobs.id"), primary_key=True)
    status = Column(String, nullable=False)
    data_json = Column(Text, nullable=False)


class FaceEnhancement(Base):
    __tablename__ = "face_enhancements"
    id = Column(String, primary_key=True, default=new_id)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=False, index=True)
    shot_number = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    data_json = Column(Text, nullable=False)
    heartbeat = Column(Float, nullable=False)
    __table_args__ = (UniqueConstraint("job_id", "shot_number", name="uq_face_job_shot"),)


class ProviderSubmissionGate(Base):
    __tablename__ = "provider_submission_gates"
    provider = Column(String, primary_key=True)
    next_at = Column(Float, nullable=False, default=0)


class PromptTechnique(Base):
    __tablename__ = "prompt_techniques"
    id = Column(String, primary_key=True, default=new_id)
    content_type = Column(String, nullable=True, index=True)
    ai_model = Column(String, nullable=True, index=True)
    technique_type = Column(String, nullable=False)
    guidance_text = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class KlingVoice(Base):
    """Reusable provider identity, separate from the character's Sarvam voice ID."""
    __tablename__ = "kling_voices"
    key = Column(String(64), primary_key=True)
    status = Column(String, nullable=False)
    data_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    original_key = Column(String, nullable=False)
    crop_key = Column(String, nullable=False)
    prepared_key = Column(String)
    accepted_key = Column(String)
    status = Column(String, nullable=False, default="uploaded")
    request_id = Column(String)
    started_at = Column(DateTime)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class JobProduct(Base):
    __tablename__ = "job_products"
    job_id = Column(String, ForeignKey("jobs.id"), primary_key=True)
    product_id = Column(String, ForeignKey("products.id"), primary_key=True)
    name = Column(String, nullable=False)
    object_key = Column(String, nullable=False)  # immutable job reference snapshot
    views_json = Column(Text, nullable=True)  # immutable approved album snapshot


class ProductView(Base):
    __tablename__ = "product_views"
    id = Column(String, primary_key=True, default=new_id)
    product_id = Column(String, ForeignKey("products.id"), nullable=False, index=True)
    request_key = Column(String, unique=True, nullable=False)
    angle = Column(String, nullable=False)
    provenance = Column(String, nullable=False)  # original | inferred
    status = Column(String, nullable=False, default="review")
    object_key = Column(String)
    thumbnail_key = Column(String)
    source_keys = Column(JSON, nullable=False, default=list)
    generation_contract = Column(JSON)
    candidate_keys = Column(JSON, nullable=False, default=list)
    model = Column(String)
    request_id = Column(String)
    status_url = Column(Text)
    response_url = Column(Text)
    verification_only = Column(Boolean, nullable=False, default=False)
    attempts = Column(Integer, nullable=False, default=0)
    verdict = Column(JSON)
    error = Column(Text)
    lease_until = Column(DateTime)
    next_poll_at = Column(DateTime)
    started_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
