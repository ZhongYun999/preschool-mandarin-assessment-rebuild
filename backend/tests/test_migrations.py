import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from dotenv import load_dotenv
from sqlalchemy import text

from app import taskbank  # noqa: F401
from app.database import Base, create_database_engine


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
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_taskbank"
            connection.commit()
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            connection.commit()
            command.downgrade(config, "base")
            functions = text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                             "WHERE n.nspname=current_schema() AND p.proname IN "
                             "('guard_task_identity','guard_referenced_asset',"
                             "'lock_revision_assets','reject_revision_mutation')")
            assert connection.scalar(functions) == 0
            connection.commit()
            command.upgrade(config, "head")
            assert connection.scalar(functions) == 4
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_taskbank"
            connection.commit()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
