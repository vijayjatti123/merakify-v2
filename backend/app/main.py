from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import Base, engine, ensure_asset_tagging_columns, ensure_job_intake_columns
from app.routes import asset_routes, character_routes, jobs, script_routes

Base.metadata.create_all(bind=engine)
ensure_job_intake_columns()
ensure_asset_tagging_columns()

app = FastAPI(title="Merakify Core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(asset_routes.router)
app.include_router(character_routes.router)
app.include_router(script_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
