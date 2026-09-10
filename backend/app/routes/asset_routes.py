import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import AssetOut, AssetRole
from app.services import asset_service, storage_service

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.post("/upload", response_model=AssetOut)
def upload_asset(
    file: UploadFile = File(...),
    role: AssetRole | None = Form(None),
    label: str | None = Form(None, max_length=120),
    db: Session = Depends(get_db),
):
    filename = Path(file.filename or "asset").name.strip() or "asset"
    normalized_label = label.strip() if label and label.strip() else None
    object_key = f"assets/{uuid.uuid4()}/{filename}"
    uploaded = storage_service.upload_file(
        object_key,
        file.file,
        content_type=file.content_type,
    )
    try:
        asset = asset_service.create_asset(
            db,
            filename=filename,
            object_key=uploaded["key"],
            url=uploaded["url"],
            role=role,
            label=normalized_label,
        )
    except Exception:
        storage_service.delete_object(uploaded["key"])
        raise
    return AssetOut(
        id=asset.id,
        filename=asset.filename,
        url=uploaded["url"],
        role=asset.role,
        label=asset.label,
        created_at=asset.created_at,
    )


@router.get("", response_model=list[AssetOut])
def list_assets(db: Session = Depends(get_db)):
    return [
        AssetOut(
            id=asset.id,
            filename=asset.filename,
            url=storage_service.asset_url(asset.object_key),
            role=asset.role,
            label=asset.label,
            created_at=asset.created_at,
        )
        for asset in asset_service.list_assets(db)
    ]
