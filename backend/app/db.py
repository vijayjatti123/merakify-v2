from sqlalchemy import create_engine
from sqlalchemy import inspect
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
    "aspect_ratio": "VARCHAR NOT NULL DEFAULT '16:9'",
    "quality": "VARCHAR NOT NULL DEFAULT '720p'",
    "language": "VARCHAR NOT NULL DEFAULT 'English'",
    "ai_model": "VARCHAR NOT NULL DEFAULT 'Seedance 2.5'",
    "script_text": "TEXT",
    "resolutions_json": "TEXT",
}

ASSET_COLUMN_DDL = {
    "role": "VARCHAR",
    "label": "VARCHAR",
}

CHARACTER_COLUMN_DDL = {
    "reference_sheet_url": "TEXT",
}


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
    """Add the nullable reference-sheet URL to existing character vaults."""
    inspector = inspect(engine)
    if "characters" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("characters")}
    with engine.begin() as connection:
        for name, definition in CHARACTER_COLUMN_DDL.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE characters ADD COLUMN {name} {definition}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
