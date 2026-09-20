from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings


def _normalized_url(url: str) -> str:
    """Railway (and most hosts) hand out a bare postgresql:// URL, which
    SQLAlchemy defaults to the psycopg2 dialect for. We install psycopg 3
    instead (broader pre-built wheel coverage, avoids the classic Railpack
    "failed to build psycopg2-binary from source" failure), so the scheme
    needs to explicitly say so. SQLite passes through untouched.
    """
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    return url


DATABASE_URL = _normalized_url(settings.database_url)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


JOB_COLUMN_DDL = {
    "ad_type": "VARCHAR NOT NULL DEFAULT 'character'",
    "ad_brief_json": "TEXT",
    "aspect_ratio": "VARCHAR NOT NULL DEFAULT '16:9'",
    "visual_style": "VARCHAR NOT NULL DEFAULT 'Natural'",
    "color_grade": "VARCHAR NOT NULL DEFAULT 'None'",
    "quality": "VARCHAR NOT NULL DEFAULT '720p'",
    "language": "VARCHAR NOT NULL DEFAULT 'English'",
    "ai_model": "VARCHAR NOT NULL DEFAULT 'Seedance 2.5'",
    "video_model": "VARCHAR",
    "script_text": "TEXT",
    "resolutions_json": "TEXT",
    "creative_direction_json": "TEXT",
}

ASSET_COLUMN_DDL = {
    "role": "VARCHAR",
    "label": "VARCHAR",
}

CHARACTER_COLUMN_DDL = {
    "reference_sheet_url": "TEXT",
    "display_name": "VARCHAR(80)",
    "catalog_status": "VARCHAR NOT NULL DEFAULT 'review_required'",
}

# Explicitly reviewed legacy IDs, not runtime name-pattern filtering.
# Meera/Tara were approved for customer use by the user; fixtures stay hidden.
REVIEWED_CHARACTER_CATALOG = {
    "0eacb44a-3b89-4598-b89d-492073a16051": ("Meera", "customer"),
    "e0e41b06-539b-40a0-b9e1-f8b73cf2aee9": ("Tara", "customer"),
    "53e9e215-e9a6-4213-ac2e-1e0c25d8c292": (None, "test"),
    "7afd9d42-49ee-4066-abfd-81c0bf3cc177": (None, "test"),
    "e9ce4d42-eabc-4385-92ad-6cbf6303e691": (None, "test"),
    "9d35e156-2c07-4290-99ad-1e3a85cf5c0e": (None, "test"),
}


def apply_reviewed_character_catalog(connection) -> None:
    for character_id, (display_name, catalog_status) in REVIEWED_CHARACTER_CATALOG.items():
        connection.execute(text(
            "UPDATE characters SET display_name=:display_name, catalog_status=:catalog_status "
            "WHERE id=:id AND catalog_status='review_required' AND display_name IS NULL"
        ), {"id": character_id, "display_name": display_name, "catalog_status": catalog_status})


def ensure_job_intake_columns() -> None:
    """Add intake columns for existing SQLite/Postgres deployments.

    SQLAlchemy's create_all creates new tables but deliberately does not alter
    an existing jobs table. This small idempotent upgrade keeps the skeleton's
    no-migration-framework setup deployable without maintaining parallel DDL.
    """
    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("jobs")}
    with engine.begin() as connection:
        for name, definition in JOB_COLUMN_DDL.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
                if name in ("visual_style", "color_grade"):
                    from app.schemas import style_from_brief

                    # Preserve Module F/H selections on pre-existing jobs, once only.
                    for row in connection.execute(text("SELECT id, brief FROM jobs")).mappings().all():
                        connection.execute(
                            text(f"UPDATE jobs SET {name} = :value WHERE id = :id"),
                            {"value": style_from_brief(row["brief"], name), "id": row["id"]},
                        )


def ensure_asset_tagging_columns() -> None:
    """Add optional tagging columns to existing asset libraries."""
    inspector = inspect(engine)
    if "assets" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("assets")}
    with engine.begin() as connection:
        for name, definition in ASSET_COLUMN_DDL.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE assets ADD COLUMN {name} {definition}")


def ensure_character_reference_sheet_column() -> None:
    """Add vault metadata idempotently; legacy identities stay hidden until reviewed.

    Approval only records the image/voice workflow. It is not evidence that an
    old record is customer content, so never infer eligibility from its name.
    """
    inspector = inspect(engine)
    if "characters" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("characters")}
    with engine.begin() as connection:
        for name, definition in CHARACTER_COLUMN_DDL.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE characters ADD COLUMN {name} {definition}")
        if "catalog_status" not in existing:
            apply_reviewed_character_catalog(connection)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_product_album_columns():
    columns = {c["name"] for c in inspect(engine).get_columns("job_products")}
    if "views_json" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE job_products ADD COLUMN views_json TEXT"))
    columns = {c["name"] for c in inspect(engine).get_columns("product_views")}
    with engine.begin() as connection:
        for name, ddl in {"status_url": "TEXT", "response_url": "TEXT", "generation_contract": "JSON",
                          "verification_only": "BOOLEAN NOT NULL DEFAULT FALSE"}.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE product_views ADD COLUMN {name} {ddl}"))
