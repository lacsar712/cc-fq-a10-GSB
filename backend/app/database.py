from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def ensure_schema() -> None:
    """Additive migrations for existing dev volumes (create_all only handles fresh DBs)."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requeued_from_id INTEGER"
        )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
