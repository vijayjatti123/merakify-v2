from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import threading

from app.config import settings
from app.routes import voice_preview_routes, product_routes
from app.db import (
    Base,
    engine,
    ensure_asset_tagging_columns,
    ensure_character_reference_sheet_column,
    ensure_job_intake_columns,
    ensure_product_album_columns,
)
from app.routes import asset_routes, character_routes, jobs, script_routes, clarifier_routes

Base.metadata.create_all(bind=engine)
ensure_job_intake_columns()
ensure_product_album_columns()
ensure_asset_tagging_columns()
ensure_character_reference_sheet_column()
from app.services.prompt_technique_service import seed_techniques
seed_techniques()

@asynccontextmanager
async def lifespan(app):
    from app.services.video_generation_service import polling_loop
    stop = threading.Event()
    worker = threading.Thread(target=polling_loop, args=(stop,), daemon=True, name="video-task-poller")
    worker.start()
    from app.services.face_enhancement_service import polling_loop as face_polling_loop
    face_worker = threading.Thread(target=face_polling_loop, args=(stop,), daemon=True, name="face-enhancement-worker")
    face_worker.start()
    from app.services.pipeline_tasks import polling_loop as pipeline_polling_loop
    pipeline_worker = threading.Thread(target=pipeline_polling_loop, args=(stop,), daemon=True, name="pipeline-dispatcher")
    pipeline_worker.start()
    yield
    stop.set()


app = FastAPI(title="Merakify Core", lifespan=lifespan)

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
app.include_router(voice_preview_routes.router)
app.include_router(clarifier_routes.router)
app.include_router(product_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
