import json
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from uuid import UUID
from sqlalchemy.orm import Session
from app.db import get_db
from app.services import product_service as products

router = APIRouter(prefix="/api/products", tags=["products"])


def find(db, ident):
    row = db.get(products.Product, ident)
    if not row:
        raise HTTPException(404, "Product not found")
    return row


@router.get("")
def list_products(db: Session = Depends(get_db)):
    return [products.public(p) for p in db.query(products.Product).order_by(products.Product.created_at.desc()).limit(100)]


@router.post("", status_code=201)
def upload(name: str = Form(...), crop: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        bounds = json.loads(crop)
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise ValueError("Choose a product area.")
        row = products.create(db, name, file.file.read(15*1024*1024+1), bounds)
        return products.public(row)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{ident}")
def get_product(ident: str, db: Session = Depends(get_db)):
    return products.public(products.refresh(db, find(db, ident)))


@router.post("/{ident}/prepare")
def prepare(ident: str, db: Session = Depends(get_db)):
    return products.public(products.prepare(db, find(db, ident)))


class Approval(BaseModel):
    version: str


@router.post("/{ident}/approve")
def approve(ident: str, payload: Approval, db: Session = Depends(get_db)):
    try:
        return products.public(products.approve(db, find(db, ident), payload.version))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{ident}/album")
def album(ident: str, db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    find(db, ident)
    return {**service.options(), "views": [service.public(row) for row in service.rows(db, ident)]}


@router.post("/{ident}/album/upload", status_code=201)
def upload_view(ident: str, angle: str = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    try:
        return service.public(service.upload(db, find(db, ident), angle, file.file.read(15*1024*1024+1)))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class AlbumPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    angles: list[Literal["front", "three_quarter", "side", "back", "top", "detail"]] = Field(min_length=1, max_length=3)
    model: Literal["pro", "standard"]
    request_key: UUID
    max_generation_usd: float = Field(ge=0, le=10, allow_inf_nan=False)


@router.post("/{ident}/album/prepare", status_code=202)
def prepare_views(ident: str, payload: AlbumPrepare, db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    try:
        return {"views": [service.public(row) for row in service.queue(db, find(db, ident), payload.angles,
            payload.model, str(payload.request_key), payload.max_generation_usd)]}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class AlbumApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view_ids: list[str] = Field(min_length=1, max_length=24)
    inferred_details_reviewed: bool = False


@router.post("/{ident}/album/approve")
def approve_views(ident: str, payload: AlbumApproval, db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    find(db, ident)
    try:
        service.approve(db, ident, payload.view_ids, payload.inferred_details_reviewed)
        return {"views": [service.public(row) for row in service.rows(db, ident)]}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{ident}/album/{view_id}/verify")
def verify_view(ident: str, view_id: str, db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    row = db.get(service.ProductView, view_id)
    if not row or row.product_id != ident:
        raise HTTPException(404, "Product view not found")
    try:
        service.retry_verification(db, row)
        return {"status": "verifying"}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{ident}/album/{view_id}/withdraw")
def withdraw_view(ident: str, view_id: str, db: Session = Depends(get_db)):
    from app.services import product_album_service as service
    row = db.get(service.ProductView, view_id)
    if not row or row.product_id != ident:
        raise HTTPException(404, "Product view not found")
    try:
        service.withdraw(db, row)
        return {"views": [service.public(r) for r in service.rows(db, ident)]}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
