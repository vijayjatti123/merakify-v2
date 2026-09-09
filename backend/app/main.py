from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import Base, engine, ensure_job_intake_columns
from app.routes import jobs

Base.metadata.create_all(bind=engine)
ensure_job_intake_columns()

app = FastAPI(title="Merakify Core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(jobs.assets_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
