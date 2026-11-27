"""Exercise shared and isolated PostgreSQL fixture lifecycle."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import _db_stats, _validated_test_url
from sqlalchemy import text
from sqlalchemy.orm import Session


@pytest.mark.parametrize("iteration", [0, 1])
def test_shared_schema_cleans_data_resets_sequence_and_keeps_head(db, iteration):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM task_banks")) == 0
        assert (
            connection.scalar(text("SELECT nextval('speaking_runtime_policy_policy_id_seq')")) == 1
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        connection.execute(
            text("INSERT INTO task_banks (bank_id, name) VALUES (:id, 'fixture')"), {"id": uuid4()}
        )
        assert connection.scalar(text("SELECT count(*) FROM task_banks")) == 1
    assert _db_stats.shared_initializations == 1


def test_db_uses_independent_committing_connections(db):
    first, second = uuid4(), uuid4()
    with db.connect() as a, db.connect() as b:
        assert a.connection.driver_connection is not b.connection.driver_connection
        a.execute(
            text("INSERT INTO task_banks (bank_id, name) VALUES (:id, 'committed')"), {"id": first}
        )
        a.commit()
        b.execute(
            text("INSERT INTO task_banks (bank_id, name) VALUES (:id, 'rolled back')"),
            {"id": second},
        )
        b.rollback()
        assert (
            b.scalar(text("SELECT count(*) FROM task_banks WHERE bank_id=:id"), {"id": first}) == 1
        )
        assert (
            b.scalar(text("SELECT count(*) FROM task_banks WHERE bank_id=:id"), {"id": second}) == 0
        )


@pytest.mark.db_isolated
def test_isolated_ddl_cannot_change_shared_schema(db, _shared_database):
    shared, _, shared_schema, _ = _shared_database
    with db.begin() as connection:
        isolated_schema = connection.scalar(text("SELECT current_schema()"))
        assert isolated_schema != shared_schema
        connection.execute(text("CREATE TABLE test_fixture_ddl (value integer)"))
    migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        migration.attributes["connection"] = connection
        command.downgrade(migration, "0025_llm_provider_config")
        connection.commit()
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0025_llm_provider_config"
        )
    with shared.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT to_regclass(:name)"), {"name": f"{shared_schema}.test_fixture_ddl"}
            )
            is None
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(
                Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            ).get_current_head()
        )


def test_failed_test_cleans_and_releases_schema(db, tmp_path):
    test_file = tmp_path / "test_child_cleanup.py"
    test_file.write_text(
        """import os\nfrom pathlib import Path\nfrom sqlalchemy import text
from sqlalchemy.orm import Session\nfrom uuid import uuid4\n\ndef test_first_fails(db):\n    with db.begin() as c:\n        Path(os.environ["FIXTURE_SCHEMA_FILE"]).write_text(c.scalar(text("SELECT current_schema()")))\n        c.execute(text("INSERT INTO task_banks (bank_id, name) VALUES (:id, 'fixture')"), {"id": uuid4()})\n    assert False, "intentional failure"\n\ndef test_second_is_clean(db):\n    with db.connect() as c:\n        assert c.scalar(text("SELECT count(*) FROM task_banks")) == 0\n        assert c.scalar(text("SELECT nextval('speaking_runtime_policy_policy_id_seq')")) == 1\n""",
        encoding="utf-8",
    )
    schema_file = tmp_path / "schema.txt"
    tests_dir = Path(__file__).resolve().parent
    env = {**os.environ, "FIXTURE_SCHEMA_FILE": str(schema_file)}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [env.get("PYTHONPATH", ""), str(tests_dir)]))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", str(test_file), "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 1
    assert "1 failed, 1 passed" in result.stdout
    assert "Fixture schema initializations: shared=1, isolated=0" in result.stdout
    schema = schema_file.read_text(encoding="utf-8")
    with db.connect() as connection:
        assert not connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
            {"schema": schema},
        )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "postgresql+psycopg://user:pass@localhost:5432/vnext_dev",
        "postgresql+psycopg://user:pass@remote.example:5432/vnext_test",
        "postgresql+psycopg://user:pass@localhost:5432/vnext_test?host=remote.example",
        "sqlite:///test.db",
    ],
)
def test_rejects_unsafe_database_targets(url, monkeypatch):
    for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="local disposable test database"):
        _validated_test_url(url)


@pytest.mark.parametrize("attempt", [0, 1])
def test_cleanup_uses_qualified_schema_even_if_search_path_changes(db, attempt):
    with db.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        quoted = connection.dialect.identifier_preparer.quote_identifier(schema)
        assert connection.scalar(text("SELECT current_setting('application_name')")) == schema
        connection.execute(text("SET application_name TO 'changed_fixture'"))
        connection.execute(text("SET search_path TO public"))
        assert connection.scalar(text(f"SELECT count(*) FROM {quoted}.task_banks")) == 0
        connection.execute(
            text(f"INSERT INTO {quoted}.task_banks (bank_id, name) VALUES (:id, 'fixture')"),
            {"id": uuid4()},
        )
        connection.commit()


@pytest.mark.db_isolated
@pytest.mark.parametrize("dependency", ["foreign_key", "view"])
def test_cleanup_refuses_external_schema_dependencies(db, dependency):
    from conftest import _clean_shared, _table_names
    from sqlalchemy import create_engine

    admin = create_engine(db.url, pool_pre_ping=True)
    external = "test_external_" + uuid4().hex
    with db.begin() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        tables = _table_names(connection, schema)
        bank_id = uuid4()
        connection.execute(
            text("INSERT INTO task_banks (bank_id, name) VALUES (:id, 'fixture')"), {"id": bank_id}
        )
    try:
        with admin.begin() as connection:
            quote = connection.dialect.identifier_preparer.quote_identifier
            connection.exec_driver_sql(f"CREATE SCHEMA {quote(external)}")
            if dependency == "foreign_key":
                connection.exec_driver_sql(
                    f"CREATE TABLE {quote(external)}.reference (bank_id uuid REFERENCES {quote(schema)}.task_banks(bank_id))"
                )
                connection.execute(
                    text(f"INSERT INTO {quote(external)}.reference (bank_id) VALUES (:id)"),
                    {"id": bank_id},
                )
            else:
                connection.exec_driver_sql(
                    f"CREATE VIEW {quote(external)}.reference AS SELECT bank_id FROM {quote(schema)}.task_banks"
                )
        with pytest.raises(RuntimeError, match="referenced outside"):
            _clean_shared(db, admin, schema, tables)
        with admin.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {quote(schema)}.task_banks")) == 1
            assert connection.scalar(text(f"SELECT count(*) FROM {quote(external)}.reference")) == 1
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {quote(external)} CASCADE")
        admin.dispose()


def test_independent_sessions_commit_and_rollback(db):
    from app.taskbank.models import TaskBank

    committed, rolled_back = uuid4(), uuid4()
    with Session(db) as first, Session(db) as second:
        assert (
            first.connection().connection.driver_connection
            is not second.connection().connection.driver_connection
        )
        first.add(TaskBank(bank_id=committed, name="committed"))
        first.commit()
        second.add(TaskBank(bank_id=rolled_back, name="rolled back"))
        second.rollback()
        assert second.get(TaskBank, committed) is not None
        assert second.get(TaskBank, rolled_back) is None


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"])
def test_libpq_environment_cannot_redirect_test_database(name, monkeypatch):
    for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        monkeypatch.delenv(key, raising=False)
    url = "postgresql+psycopg://user:pass@localhost:5432/vnext_test"
    assert _validated_test_url(url) == url
    monkeypatch.setenv(name, "unexpected")
    with pytest.raises(ValueError, match="local disposable test database"):
        _validated_test_url(url)
