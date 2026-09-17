import json
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
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
