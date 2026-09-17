from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import SessionLocal
from app.schemas import JobCreate
from app.services import clarifier_service as service, job_service as storage

router = APIRouter(prefix="/api/clarifier", tags=["clarifier"])


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_brief: str = Field(min_length=1, max_length=20000)
    known_fields: dict[str, Any] = Field(default_factory=dict)
    input_mode: Literal["idea", "script"] = "idea"
    product_ids: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_fields(self):
        # Six exact JobCreate keys; duration/content_type are currently prose
        # mirrors in NewJob, not typed JobCreate fields. Do not alter that API.
        keys = {"duration", "aspect_ratio", "content_type", "color_grade", "visual_style",
                "quality", "language", "ai_model", "tone", "differentiator", "constraints"}
        if not self.raw_brief.strip() or self.known_fields.keys() - keys:
            raise ValueError("Invalid brief or unknown known_fields key")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in self.known_fields.values()):
            raise ValueError("Known fields must be non-empty strings")
        JobCreate(brief=self.raw_brief, **{k: v for k, v in self.known_fields.items() if k in JobCreate.model_fields})
        return self


class Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


class Answer(Write):
    answer: str = Field(min_length=1, max_length=8000, pattern=r"\S")


class Edit(Write):
    refined_prompt: str = Field(min_length=1, max_length=20000, pattern=r"\S")


@router.post("/start", status_code=201)
def start(body: Start):
    with SessionLocal() as db:
        from app.services.product_service import selected
        try:
            products = selected(db, body.product_ids)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        context = {"input_mode": body.input_mode,
            "products": [{"id": p.id, "name": p.name} for p in products]}
        return service.snapshot(service.start(db, body.raw_brief, body.known_fields, context))


@router.get("/{session_id}")
def get_session(session_id: str):
    with SessionLocal() as db:
        row = storage.get_clarifier_session(db, session_id)
        if row is None:
            raise HTTPException(404, "Session not found")
        return service.snapshot(row)


def mutate(session_id, body, action, value=None):
    with SessionLocal() as db:
        row = storage.get_clarifier_session(db, session_id)
        if row is None:
            raise HTTPException(404, "Session not found")
        if row.revision != body.revision:
            raise HTTPException(409, "Session changed; reload it before retrying.")
        try:
            return service.snapshot(service.act(db, row, action, value))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/{session_id}/answer")
def answer(session_id: str, body: Answer):
    return mutate(session_id, body, "answer", body.answer)


@router.post("/{session_id}/refine")
def refine(session_id: str, body: Write):
    return mutate(session_id, body, "refine")


@router.post("/{session_id}/edit")
def edit(session_id: str, body: Edit):
    return mutate(session_id, body, "edit", body.refined_prompt)


@router.post("/{session_id}/cancel")
def cancel(session_id: str, body: Write):
    return mutate(session_id, body, "cancel")
