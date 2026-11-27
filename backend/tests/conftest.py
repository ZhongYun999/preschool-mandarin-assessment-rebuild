import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

from app.taskbank.models import TaskBank


@dataclass
class DatabaseStats:
    shared_initializations: int = 0
    isolated_initializations: int = 0
    shared_upgrades: int = 0
    isolated_upgrades: int = 0
    shared_upgrade_seconds: float = 0.0
    isolated_upgrade_seconds: float = 0.0
    cleanups: int = 0
    cleanup_failures: int = 0
    cleanup_seconds: float = 0.0


_db_stats = DatabaseStats()
_active_kind = None
_initialization_kind = None
_shared_broken = False
_original_upgrade = None


def _validated_test_url(raw):
    try:
        parsed = make_url(raw)
    except (ArgumentError, ValueError, TypeError):
        raise ValueError(
            "TEST_DATABASE_URL must identify a local disposable test database"
        ) from None
    name = parsed.database or ""
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or not (name.startswith("test_") or name.endswith("_test"))
        or parsed.query
        or any(
            os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
        )
    ):
        raise ValueError("TEST_DATABASE_URL must identify a local disposable test database")
    return raw


@pytest.fixture(scope="session")
def _test_database_url():
    load_dotenv()
    try:
        return _validated_test_url(os.environ.get("TEST_DATABASE_URL"))
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)


def _table_names(connection, schema):
    return tuple(
        connection.scalars(
            text("""
        SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')
          AND c.relname <> 'alembic_version'
        ORDER BY c.relname
    """),
            {"schema": schema},
        )
    )


def _has_external_relations(connection, schema):
    return connection.scalar(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint fk
                JOIN pg_catalog.pg_class parent ON parent.oid = fk.confrelid
                JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                JOIN pg_catalog.pg_class child ON child.oid = fk.conrelid
                JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
                WHERE fk.contype = 'f' AND parent_ns.nspname = :schema
                  AND child_ns.nspname <> :schema
                UNION ALL
                SELECT 1 FROM pg_catalog.pg_inherits inheritance
                JOIN pg_catalog.pg_class parent ON parent.oid = inheritance.inhparent
                JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                JOIN pg_catalog.pg_class child ON child.oid = inheritance.inhrelid
                JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
                WHERE parent_ns.nspname = :schema AND child_ns.nspname <> :schema
                UNION ALL
                SELECT 1 FROM pg_catalog.pg_depend dependency
                JOIN pg_catalog.pg_rewrite rule ON rule.oid = dependency.objid
                  AND dependency.classid = 'pg_rewrite'::regclass
                JOIN pg_catalog.pg_class child ON child.oid = rule.ev_class
                JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
                JOIN pg_catalog.pg_class parent ON parent.oid = dependency.refobjid
                  AND dependency.refclassid = 'pg_class'::regclass
                JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                WHERE parent_ns.nspname = :schema AND child_ns.nspname <> :schema
            )
        """),
        {"schema": schema},
    )


@contextmanager
def _temporary_schema(url, kind):
    global _initialization_kind
    schema = "test_" + uuid4().hex
    parsed = make_url(url)
    address = "::1" if parsed.host == "::1" else "127.0.0.1"
    admin = create_engine(url, pool_pre_ping=True, connect_args={"hostaddr": address})
    engine = None
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(url, pool_pre_ping=True, connect_args={"hostaddr": address})

        @event.listens_for(engine, "connect")
        def set_schema(dbapi_connection, connection_record):
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute("SELECT set_config('application_name', %s, false)", (schema,))
            dbapi_connection.commit()

        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            _initialization_kind = kind
            try:
                command.upgrade(config, "head")
            finally:
                _initialization_kind = None
            connection.commit()
            tables = _table_names(connection, schema)
        if kind == "shared":
            _db_stats.shared_initializations += 1
        else:
            _db_stats.isolated_initializations += 1
        yield engine, admin, schema, tables
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.begin() as connection:
                    if _has_external_relations(connection, schema):
                        raise RuntimeError("Refusing to drop a test schema referenced externally")
                    connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _clean_shared(engine, admin, schema, expected_tables):
    try:
        # Dispose idle connections first, releasing session-level state and locks.
        engine.dispose()
        with admin.begin() as connection:
            actual = _table_names(connection, schema)
            if actual != expected_tables:
                raise RuntimeError(
                    "Shared test schema structure changed; mark this test db_isolated"
                )
            if _has_external_relations(connection, schema):
                raise RuntimeError("Refusing to truncate tables referenced outside the test schema")
            if actual:
                quote = connection.dialect.identifier_preparer.quote_identifier
                tables = ", ".join(f"{quote(schema)}.{quote(name)}" for name in actual)
                connection.exec_driver_sql(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def _shared_database(_test_database_url):
    with _temporary_schema(_test_database_url, "shared") as shared:
        yield shared


@pytest.fixture
def db(request, monkeypatch, _test_database_url):
    global _shared_broken
    monkeypatch.setenv("DATABASE_URL", _test_database_url)
    if request.node.get_closest_marker("db_isolated"):
        with _temporary_schema(_test_database_url, "isolated") as isolated:
            yield isolated[0]
    else:
        if _shared_broken:
            raise RuntimeError("Shared test schema cleanup previously failed")
        engine, admin, schema, tables = request.getfixturevalue("_shared_database")
        try:
            yield engine
        finally:
            started = time.perf_counter()
            try:
                _clean_shared(engine, admin, schema, tables)
            except BaseException:
                _db_stats.cleanup_failures += 1
                _shared_broken = True
                raise
            finally:
                _db_stats.cleanups += 1
                _db_stats.cleanup_seconds += time.perf_counter() - started


@pytest.fixture
def bank(db):
    bank_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=bank_id, name="测试题库"))
    return bank_id


def pytest_configure(config):
    global _original_upgrade
    if hasattr(config, "workerinput") or getattr(config.option, "numprocesses", None):
        raise pytest.UsageError("Shared test schemas do not support pytest-xdist")
    _original_upgrade = command.upgrade

    def counted_upgrade(*args, **kwargs):
        kind = _initialization_kind or _active_kind
        if kind == "isolated":
            _db_stats.isolated_upgrades += 1
        elif kind == "shared":
            _db_stats.shared_upgrades += 1
        started = time.perf_counter()
        try:
            return _original_upgrade(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            if kind == "isolated":
                _db_stats.isolated_upgrade_seconds += elapsed
            elif kind == "shared":
                _db_stats.shared_upgrade_seconds += elapsed

    command.upgrade = counted_upgrade


def pytest_unconfigure(config):
    if _original_upgrade is not None:
        command.upgrade = _original_upgrade


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _active_kind
    _active_kind = "isolated" if item.get_closest_marker("db_isolated") else "shared"
    try:
        yield
    finally:
        _active_kind = None


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_sep("-", "PostgreSQL test database")
    terminalreporter.write_line(
        f"Fixture schema initializations: shared={_db_stats.shared_initializations}, "
        f"isolated={_db_stats.isolated_initializations}; "
        f"Alembic upgrades: shared={_db_stats.shared_upgrades} "
        f"({_db_stats.shared_upgrade_seconds:.2f}s), isolated={_db_stats.isolated_upgrades} "
        f"({_db_stats.isolated_upgrade_seconds:.2f}s)"
    )
    terminalreporter.write_line(
        f"Shared cleanup: {_db_stats.cleanups} tests, {_db_stats.cleanup_failures} failures, "
        f"{_db_stats.cleanup_seconds:.2f}s total"
    )
