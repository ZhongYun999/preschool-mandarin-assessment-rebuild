import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.taskbank import (
    Asset,
    RevisionContent,
    TaskBank,
    TaskIdentity,
    TaskRevision,
    append_revision,
)

CONTENT = RevisionContent(grade_level="测试学段", task_type="qa", prompt_text="题目甲")


def write(db, bank, content=CONTENT, code="0001", order=1, images=None):
    with Session(db) as session, session.begin():
        return append_revision(session, bank_id=bank, task_code=code, display_order=order,
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
        assert s.get(TaskRevision, first.task_revision_id).option_a_asset_id == a
        assert s.get(TaskRevision, changed.task_revision_id).option_a_asset_id == b


def test_raw_sql_cannot_modify_historical_revisions_links_or_referenced_asset(db, bank):
    asset_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(Asset(asset_id=asset_id, sha256="c" * 64, storage_ref="immutable", media_type="image/png"))
    rev = write(db, bank, images={"option_A": asset_id})
    statements = [
        ("UPDATE task_revisions SET prompt_text = 'x' WHERE task_revision_id = :id", rev.task_revision_id),
        ("DELETE FROM task_revisions WHERE task_revision_id = :id", rev.task_revision_id),
        ("UPDATE task_revisions SET option_a_asset_id = NULL WHERE task_revision_id = :id", rev.task_revision_id),
        ("UPDATE task_revisions SET option_b_asset_id = option_a_asset_id WHERE task_revision_id = :id", rev.task_revision_id),
        ("UPDATE assets SET storage_ref = 'x' WHERE asset_id = :id", asset_id),
        ("UPDATE assets SET sha256 = repeat('d', 64) WHERE asset_id = :id", asset_id),
        ("UPDATE assets SET media_type = 'x' WHERE asset_id = :id", asset_id),
        ("DELETE FROM assets WHERE asset_id = :id", asset_id),
    ]
    for sql, identifier in statements:
        with pytest.raises(DBAPIError), db.begin() as connection:
            connection.execute(text(sql), {"id": identifier})



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
                "(task_revision_id,task_id,version,grade_level,task_type,prompt_text,content_hash,option_a_asset_id) "
                "VALUES (:rev,:task,2,'x','qa','x','test',:asset)"
            ), {"rev": revision_id, "task": task.task_id, "asset": asset_id})
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


def test_revision_image_created_inside_savepoint(db, bank):
    ids = [uuid4() for _ in range(4)]
    with Session(db) as s, s.begin():
        s.add_all(Asset(asset_id=asset, sha256=str(i + 1) * 64,
                        storage_ref=f"savepoint-{i}", media_type="image/png")
                  for i, asset in enumerate(ids))
    images = dict(zip(("option_A", "option_B", "option_C", "question_image"), ids, strict=True))
    with Session(db) as s, s.begin():
        with s.begin_nested():
            result = append_revision(s, bank_id=bank, task_code="savepoint", display_order=1,
                                     content=CONTENT, images=images)
        assert s.get(TaskRevision, result.task_revision_id).question_image_asset_id == ids[3]
    with Session(db) as s:
        revision = s.get(TaskRevision, result.task_revision_id)
        assert [revision.option_a_asset_id, revision.option_b_asset_id, revision.option_c_asset_id,
                revision.question_image_asset_id] == ids
    for column in ("option_a_asset_id", "option_b_asset_id", "option_c_asset_id", "question_image_asset_id"):
        for new_asset in (None, ids[0]):
            with pytest.raises(DBAPIError), db.begin() as connection:
                connection.execute(text(f"UPDATE task_revisions SET {column} = :asset WHERE task_revision_id = :id"),
                                   {"asset": new_asset, "id": result.task_revision_id})


def test_outer_transaction_rolls_back_all_appends_and_order_on_failure(db, bank):
    existing = write(db, bank, order=3)
    with pytest.raises(ValueError, match="Image asset does not exist"), Session(db) as s, s.begin():
        append_revision(s, bank_id=bank, task_code="0001", display_order=99,
                        content=replace(CONTENT, prompt_text="new"), images={})
        append_revision(s, bank_id=bank, task_code="0002", display_order=4,
                        content=CONTENT, images={})
        append_revision(s, bank_id=bank, task_code="0003", display_order=5,
                        content=CONTENT, images={"question_image": uuid4()})
    with Session(db) as s:
        assert s.get(TaskIdentity, existing.task_id).display_order == 3
        assert len(s.scalars(select(TaskIdentity)).all()) == 1
        revisions = s.scalars(select(TaskRevision)).all()
        assert len(revisions) == 1 and revisions[0].task_revision_id == existing.task_revision_id


def test_taskbank_description_is_optional_and_persisted(db):
    ids = [uuid4(), uuid4()]
    with Session(db) as s, s.begin():
        s.add_all([TaskBank(bank_id=ids[0], name="无说明"),
                   TaskBank(bank_id=ids[1], name="有说明", description="题库说明")])
    with Session(db) as s:
        assert s.get(TaskBank, ids[0]).description is None
        assert s.get(TaskBank, ids[1]).description == "题库说明"


def test_repeated_appends_within_one_transaction_compare_latest(db, bank):
    with Session(db) as s, s.begin():
        first = append_revision(s, bank_id=bank, task_code="batch", display_order=1,
                                content=CONTENT, images={})
        second = append_revision(s, bank_id=bank, task_code="batch", display_order=2,
                                 content=replace(CONTENT, prompt_text="B"), images={})
        third = append_revision(s, bank_id=bank, task_code="batch", display_order=3,
                                content=CONTENT, images={})
        assert [first.version, second.version, third.version] == [1, 2, 3]
    with Session(db) as s:
        assert s.get(TaskIdentity, first.task_id).display_order == 3
        assert len(s.scalars(select(TaskRevision)).all()) == 3


def test_caller_session_refreshes_locked_identity_and_assets(db, bank):
    task = write(db, bank, order=1)
    asset_id, equivalent_id = uuid4(), uuid4()
    with Session(db) as s, s.begin():
        s.add_all([Asset(asset_id=asset_id, sha256="a" * 64, storage_ref="asset", media_type="image/png"),
                   Asset(asset_id=equivalent_id, sha256="b" * 64, storage_ref="equivalent", media_type="image/png")])
    with Session(db) as stale, stale.begin():
        cached_task = stale.get(TaskIdentity, task.task_id)
        cached_asset = stale.get(Asset, asset_id)
        assert cached_task.display_order == 1 and cached_asset.sha256 == "a" * 64
        with db.begin() as other:
            other.execute(text("UPDATE task_identities SET display_order=9 WHERE task_id=:id"), {"id": task.task_id})
            other.execute(text("UPDATE assets SET sha256=repeat('b', 64) WHERE asset_id=:id"), {"id": asset_id})
        result = append_revision(stale, bank_id=bank, task_code="0001", display_order=1,
                                 content=CONTENT, images={"option_A": asset_id})
        assert result.version == 2
    same = write(db, bank, images={"option_A": equivalent_id})
    assert same.version == 2 and not same.created
    with Session(db) as s:
        assert s.get(TaskIdentity, task.task_id).display_order == 1
