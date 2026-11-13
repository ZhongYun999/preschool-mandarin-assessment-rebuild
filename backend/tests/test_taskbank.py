import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.taskbank import (
    Asset,
    RevisionContent,
    TaskBank,
    TaskIdentity,
    TaskRevision,
    TaskRevisionAsset,
    append_revision,
)


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


CONTENT = RevisionContent(grade_level="测试学段", task_type="qa", prompt_text="题目甲")


def write(db, bank, content=CONTENT, code="0001", order=1, images=None):
    return append_revision(db, bank_id=bank, task_code=code, display_order=order,
                           content=content, images=images or {})


def test_identity_unique_per_bank_and_leading_zero(db, bank):
    other = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=other, name="另一个题库"))
    first = write(db, bank)
    second = write(db, other)
    assert first.task_id != second.task_id
    with Session(db) as s:
        assert s.get(TaskIdentity, first.task_id).task_code == "0001"
    with pytest.raises(IntegrityError), Session(db) as s, s.begin():
        s.add(TaskIdentity(bank_id=bank, task_code="0001"))
        s.flush()


def test_append_only_current_comparison_and_order(db, bank):
    a = write(db, bank)
    same = write(db, bank, order=10)
    b = write(db, bank, replace(CONTENT, prompt_text="题目乙"), order=10)
    again = write(db, bank, order=10)
    assert (a.version, same.version, b.version, again.version) == (1, 1, 2, 3)
    assert same.created is False and a.task_revision_id == same.task_revision_id
    with Session(db) as s:
        revisions = s.scalars(select(TaskRevision).order_by(TaskRevision.version)).all()
        assert [r.prompt_text for r in revisions] == ["题目甲", "题目乙", "题目甲"]
        assert revisions[0].content_hash == revisions[2].content_hash
        assert s.get(TaskIdentity, a.task_id).display_order == 10


@pytest.mark.parametrize("field,value", [
    ("grade_level", "另一学段"), ("task_type", "其他题型"), ("guide_text", "引导"),
    ("prompt_text", "新题干"), ("target_rounds", 7), ("material_description", "说明"),
    ("reference_answer", "参考"), ("scoring_instruction", "评分说明"),
    ("ai_followup_scope", "追问"),
])
def test_each_content_field_creates_revision(db, bank, field, value):
    write(db, bank)
    result = write(db, bank, replace(CONTENT, **{field: value}))
    assert result.created and result.version == 2


def test_image_digest_not_asset_identity(db, bank):
    a, same, b = uuid4(), uuid4(), uuid4()
    with Session(db) as s, s.begin():
        s.add_all([Asset(asset_id=a, sha256="a" * 64, storage_ref="a", media_type="image/png"),
                   Asset(asset_id=same, sha256="a" * 64, storage_ref="copy", media_type="image/png"),
                   Asset(asset_id=b, sha256="b" * 64, storage_ref="b", media_type="image/png")])
    first = write(db, bank, images={"option_A": a})
    equal = write(db, bank, images={"option_A": same})
    changed = write(db, bank, images={"option_A": b})
    assert (first.version, equal.version, changed.version) == (1, 1, 2)
    with Session(db) as s:
        assert s.get(TaskRevisionAsset, (first.task_revision_id, "option_A")).asset_id == a
        assert s.get(TaskRevisionAsset, (changed.task_revision_id, "option_A")).asset_id == b


def test_raw_sql_cannot_modify_historical_revisions_links_or_referenced_asset(db, bank):
    asset_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(Asset(asset_id=asset_id, sha256="c" * 64, storage_ref="immutable", media_type="image/png"))
    rev = write(db, bank, images={"option_A": asset_id})
    statements = [
        ("UPDATE task_revisions SET prompt_text = 'x' WHERE task_revision_id = :id", rev.task_revision_id),
        ("DELETE FROM task_revisions WHERE task_revision_id = :id", rev.task_revision_id),
        ("UPDATE task_revision_assets SET slot = 'option_B' WHERE task_revision_id = :id", rev.task_revision_id),
        ("DELETE FROM task_revision_assets WHERE task_revision_id = :id", rev.task_revision_id),
        ("UPDATE assets SET storage_ref = 'x' WHERE asset_id = :id", asset_id),
        ("UPDATE assets SET sha256 = repeat('d', 64) WHERE asset_id = :id", asset_id),
        ("UPDATE assets SET media_type = 'x' WHERE asset_id = :id", asset_id),
        ("DELETE FROM assets WHERE asset_id = :id", asset_id),
    ]
    for sql, identifier in statements:
        with pytest.raises(DBAPIError), db.begin() as connection:
            connection.execute(text(sql), {"id": identifier})
    with pytest.raises(DBAPIError), db.begin() as connection:
        connection.execute(text("INSERT INTO task_revision_assets VALUES (:r, 'option_B', :a)"),
                           {"r": rev.task_revision_id, "a": asset_id})


def test_dialogue_rounds_are_positive_and_not_defaulted(db, bank):
    for rounds in (None, 0, -1):
        with pytest.raises(IntegrityError):
            write(db, bank, replace(CONTENT, task_type="dialogue", target_rounds=rounds))
    result = write(db, bank, replace(CONTENT, task_type="dialogue", target_rounds=20))
    assert result.version == 1


@pytest.mark.parametrize("initial,contents,expected_versions", [
    (False, ("same", "same"), [1, 1]),
    (True, ("same", "same"), [2, 2]),
    (True, ("first", "second"), [2, 3]),
])
def test_parallel_writers_use_distinct_connections(db, bank, initial, contents, expected_versions):
    existing = write(db, bank) if initial else None
    barrier = Barrier(3, timeout=10)

    def worker(prompt):
        barrier.wait()
        return write(db, bank, replace(CONTENT, prompt_text=prompt))

    with ThreadPoolExecutor(max_workers=2) as executor:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            if existing:
                blocker.execute(select(TaskIdentity.task_id).where(
                    TaskIdentity.task_id == existing.task_id).with_for_update())
            else:
                blocker.execute(select(TaskBank.bank_id).where(
                    TaskBank.bank_id == bank).with_for_update())
            futures = [executor.submit(worker, prompt) for prompt in contents]
            barrier.wait()
            deadline = time.monotonic() + 10
            blocked_count = 0
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked_count = observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name = :schema AND cardinality(pg_blocking_pids(pid)) > 0"
                    ), {"schema": schema})
                    if blocked_count >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
            assert blocked_count >= 2, "both writers must reach database lock contention"
        results = [future.result(timeout=10) for future in futures]
    assert sorted(result.version for result in results) == expected_versions
    assert results[0].task_id == results[1].task_id
    with Session(db) as s:
        assert len(s.scalars(select(TaskIdentity)).all()) == 1
        rows = s.scalars(select(TaskRevision).order_by(TaskRevision.version)).all()
        assert [r.version for r in rows] == list(range(1, max(expected_versions) + 1))
        if contents[0] != contents[1]:
            assert {r.prompt_text for r in rows[1:]} == set(contents)


def test_waiting_writer_is_blocked_by_identity_row_lock(db, bank):
    first = write(db, bank)
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.begin() as connection:
            blocker = connection.scalar(text("SELECT pg_backend_pid()"))
            connection.execute(
                select(TaskIdentity.task_id).where(TaskIdentity.task_id == first.task_id).with_for_update()
            )

            def worker():
                started.set()
                return write(db, bank, replace(CONTENT, prompt_text="锁等待后写入"))

            future = executor.submit(worker)
            assert started.wait(5)
            deadline = time.monotonic() + 10
            blocked = False
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                    ), {"pid": blocker})
                    if blocked:
                        break
                    observer.commit()
                    time.sleep(0.05)
            assert blocked, "second database connection did not wait for the identity row lock"
            assert not future.done()
        assert future.result(timeout=10).version == 2


def test_identity_cannot_be_moved_or_renamed(db, bank):
    task = write(db, bank)
    with pytest.raises(DBAPIError), db.begin() as connection:
        connection.execute(text("UPDATE task_identities SET task_code = 'different' WHERE task_id = :id"),
                           {"id": task.task_id})


def test_asset_update_racing_with_first_reference_is_rejected(db, bank):
    asset_id = uuid4()
    task = write(db, bank)
    revision_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(Asset(asset_id=asset_id, sha256="e" * 64, storage_ref="initial", media_type="image/png"))
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.begin() as connection:
            connection.execute(text(
                "INSERT INTO task_revisions "
                "(task_revision_id,task_id,version,grade_level,task_type,prompt_text,content_hash) "
                "VALUES (:rev,:task,2,'x','qa','x','test')"
            ), {"rev": revision_id, "task": task.task_id})
            connection.execute(text("INSERT INTO task_revision_assets VALUES (:rev, 'option_A', :asset)"),
                               {"rev": revision_id, "asset": asset_id})
            blocker = connection.scalar(text("SELECT pg_backend_pid()"))

            def updater():
                started.set()
                with db.begin() as other:
                    other.execute(text("UPDATE assets SET storage_ref='changed' WHERE asset_id=:id"),
                                  {"id": asset_id})

            future = executor.submit(updater)
            assert started.wait(5)
            deadline = time.monotonic() + 10
            blocked = False
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                    ), {"pid": blocker})
                    if blocked:
                        break
                    observer.commit()
                    time.sleep(0.05)
            assert blocked
        with pytest.raises(DBAPIError):
            future.result(timeout=10)
    with Session(db) as s:
        assert s.get(Asset, asset_id).storage_ref == "initial"


def test_failed_append_rolls_back_order_and_revision(db, bank):
    first = write(db, bank, order=3)
    with pytest.raises(ValueError, match="Image asset does not exist"):
        write(db, bank, replace(CONTENT, prompt_text="changed"), order=99,
              images={"question_image": uuid4()})
    with Session(db) as s:
        assert s.get(TaskIdentity, first.task_id).display_order == 3
        assert len(s.scalars(select(TaskRevision)).all()) == 1


def test_unreferenced_asset_can_be_updated_and_deleted(db):
    asset_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(Asset(asset_id=asset_id, sha256="f" * 64, storage_ref="temporary", media_type="image/png"))
    with db.begin() as connection:
        connection.execute(text("UPDATE assets SET storage_ref='updated' WHERE asset_id=:id"),
                           {"id": asset_id})
        connection.execute(text("DELETE FROM assets WHERE asset_id=:id"), {"id": asset_id})
