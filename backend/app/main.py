from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.api import router
from app.database import Base, engine


def _ensure_schema_upgrades() -> None:
    """Lightweight in-place upgrades for existing volumes (create_all won't alter)."""
    with engine.begin() as conn:
        job_cols = {c["name"] for c in inspect(conn).get_columns("jobs")}
        if "retry_of_job_id" not in job_cols:
            conn.execute(
                text("ALTER TABLE jobs ADD COLUMN retry_of_job_id INTEGER REFERENCES jobs(id)")
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_schema_upgrades()
    yield


app = FastAPI(title="FASTQ QC Pipeline Console", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
