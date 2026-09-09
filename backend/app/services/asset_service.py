from sqlalchemy.orm import Session

from app.models import Asset


def create_asset(db: Session, *, filename: str, object_key: str, url: str) -> Asset:
    asset = Asset(filename=filename, object_key=object_key, url=url)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def list_assets(db: Session) -> list[Asset]:
    return db.query(Asset).order_by(Asset.created_at.desc()).all()
