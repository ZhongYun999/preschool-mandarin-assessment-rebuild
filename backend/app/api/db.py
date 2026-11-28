"""Database sessions shared by HTTP dependencies."""

from functools import lru_cache

from sqlalchemy.orm import Session

from app.database import create_database_engine


@lru_cache(maxsize=1)
def _engine():
    return create_database_engine()


def get_db():
    with Session(_engine()) as session:
        yield session


def get_auth_db():
    """Provide a distinct Session even when FastAPI caches business dependencies."""
    with Session(_engine()) as session:
        yield session
