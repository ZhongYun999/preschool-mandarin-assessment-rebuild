import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.taskbank import TaskBank


@pytest.fixture
def db(monkeypatch):
    load_dotenv()
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL must point to a disposable PostgreSQL test database")
    monkeypatch.setenv("DATABASE_URL", url)
    admin = create_database_engine()
    schema = "test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_database_engine()

    @event.listens_for(engine, "connect")
    def set_schema(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT set_config('application_name', %s, false)", (schema,))
        dbapi_connection.commit()

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def bank(db):
    bank_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=bank_id, name="测试题库"))
    return bank_id
