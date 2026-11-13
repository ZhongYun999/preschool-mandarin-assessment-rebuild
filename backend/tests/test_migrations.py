import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text

from app.database import create_database_engine


def test_postgres_connection_and_migration_cycle(monkeypatch):
    load_dotenv()
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL must point to a disposable PostgreSQL test database")
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_database_engine()
    schema = "test_" + uuid4().hex
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_baseline"
            connection.commit()
            command.downgrade(config, "base")
            command.upgrade(config, "head")
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_baseline"
            connection.commit()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
