import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_taskbank import CONTENT, write
from test_taskbank_import import commit, xlsx

from app.paper import (
    Paper,
    PaperEditConflict,
    PaperEntry,
    add_entry,
    copy_paper,
    create_draft,
    remove_entry,
    reorder_entries,
    replace_entry,
    update_draft,
)
from app.taskbank import TaskBank, TaskIdentity, TaskRevision


def draft(db, bank):
    with Session(db) as s, s.begin():
        return create_draft(s, bank_id=bank, name="测试卷", description="说明").paper_id


def entries(s, paper_id):
    return s.scalars(
        select(PaperEntry).where(PaperEntry.paper_id == paper_id).order_by(PaperEntry.position)
    ).all()


def test_excel_import_selected_revision_stays_pinned(db, bank, tmp_path):
    result = commit(db, tmp_path, bank, xlsx(), "paper-import")
    task_id = UUID(result["items"][0]["task_id"])
    revision_id = UUID(result["items"][0]["revision_id"])
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        entry = add_entry(s, paper_id=paper_id, expected_version=0, task_id=task_id, position=1)
        assert entry.task_revision_id == revision_id
    commit(db, tmp_path, bank, xlsx(prompt="更新题干"), "paper-import-2")
    with Session(db) as s:
        assert entries(s, paper_id)[0].task_revision_id == revision_id
        assert s.get(TaskRevision, revision_id).version == 1


def test_add_remove_replace_reorder_and_transaction_rollback(db, bank):
    a = write(db, bank, code="a", order=27)
    b = write(db, bank, code="b", order=28)
    c = write(db, bank, code="c", order=29)
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        first = add_entry(
            s, paper_id=paper_id, expected_version=0, task_id=a.task_id, position=1
        ).paper_entry_id
        second = add_entry(
            s, paper_id=paper_id, expected_version=1, task_id=b.task_id, position=2
        ).paper_entry_id
        reorder_entries(s, paper_id=paper_id, expected_version=2, entry_ids=[second, first])
        replace_entry(s, paper_id=paper_id, expected_version=3, entry_id=first, task_id=c.task_id)
        update_draft(s, paper_id=paper_id, expected_version=4, name="新名称", description=None)
    with Session(db) as s:
        assert [
            (e.paper_entry_id, e.task_revision_id, e.position) for e in entries(s, paper_id)
        ] == [(second, b.task_revision_id, 1), (first, c.task_revision_id, 2)]
        assert [s.get(TaskIdentity, t.task_id).display_order for t in (a, b, c)] == [27, 28, 29]
        assert len(s.scalars(select(TaskRevision)).all()) == 3
    with pytest.raises(ValueError), Session(db) as s, s.begin():
        remove_entry(s, paper_id=paper_id, expected_version=5, entry_id=second)
        reorder_entries(s, paper_id=paper_id, expected_version=6, entry_ids=[first, second])
    with Session(db) as s:
        assert len(entries(s, paper_id)) == 2
        assert s.get(Paper, paper_id).edit_version == 5
    with Session(db) as s, s.begin():
        remove_entry(s, paper_id=paper_id, expected_version=5, entry_id=second)
    with Session(db) as s:
        assert [e.paper_entry_id for e in entries(s, paper_id)] == [first]


def test_copy_preserves_history_order_plugin_and_explicit_replacement(db, bank):
    old = write(db, bank)
    other = write(db, bank, code="other")
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        p = s.get(Paper, paper_id)
        p.scoring_plugin_id, p.scoring_plugin_version = "historical-test-plugin", "v1"
        add_entry(s, paper_id=paper_id, expected_version=0, task_id=old.task_id, position=20)
        add_entry(s, paper_id=paper_id, expected_version=1, task_id=other.task_id, position=30)
    write(db, bank, replace(CONTENT, prompt_text="B"))
    newest = write(db, bank)
    with Session(db) as s, s.begin():
        copied_id = copy_paper(s, source_id=paper_id).paper_id
    with Session(db) as s:
        p = s.get(Paper, copied_id)
        copied = entries(s, copied_id)
        assert p.status == "DRAFT" and (p.scoring_plugin_id, p.scoring_plugin_version) == (
            "historical-test-plugin",
            "v1",
        )
        assert [(e.task_revision_id, e.position) for e in copied] == [
            (old.task_revision_id, 20),
            (other.task_revision_id, 30),
        ]
        copied_entry_id = copied[0].paper_entry_id
    with Session(db) as s, s.begin():
        replace_entry(
            s, paper_id=copied_id, expected_version=0, entry_id=copied_entry_id, task_id=old.task_id
        )
    with Session(db) as s:
        assert entries(s, copied_id)[0].task_revision_id == newest.task_revision_id
        assert entries(s, paper_id)[0].task_revision_id == old.task_revision_id


def test_cross_bank_candidate_and_sql_rejected(db, bank):
    other = uuid4()
    with Session(db) as s, s.begin():
        s.add(TaskBank(bank_id=other, name="另一题库"))
    a, b = write(db, bank), write(db, other)
    paper_id = draft(db, bank)
    with pytest.raises(ValueError, match="bank"), Session(db) as s, s.begin():
        add_entry(s, paper_id=paper_id, expected_version=0, task_id=b.task_id, position=1)
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(
            text("INSERT INTO paper_entries VALUES (:id, :paper, :revision, 1)"),
            {"id": uuid4(), "paper": paper_id, "revision": b.task_revision_id},
        )
    with Session(db) as s, s.begin():
        assert (
            add_entry(
                s, paper_id=paper_id, expected_version=0, task_id=a.task_id, position=1
            ).task_revision_id
            == a.task_revision_id
        )


def test_published_facts_protected_from_sql_and_service(db, bank):
    task = write(db, bank)
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        entry_id = add_entry(
            s, paper_id=paper_id, expected_version=0, task_id=task.task_id, position=1
        ).paper_entry_id
    newer = write(db, bank, replace(CONTENT, prompt_text="更新版本"))
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE papers SET scoring_plugin_id='test', scoring_plugin_version='v1' WHERE paper_id=:id"
            ),
            {"id": paper_id},
        )
        c.execute(
            text(
                "INSERT INTO evaluation_plans (paper_entry_id, task_revision_id, plugin_id, plugin_version, requirements) VALUES (:entry, :revision, 'test', 'v1', '[]')"
            ),
            {"entry": entry_id, "revision": task.task_revision_id},
        )
        c.execute(text("UPDATE papers SET status='PUBLISHED' WHERE paper_id=:id"), {"id": paper_id})
    statements = [
        ("UPDATE paper_entries SET position=2 WHERE paper_entry_id=:id", entry_id),
        ("UPDATE paper_entries SET task_revision_id=:revision WHERE paper_entry_id=:id", entry_id),
        ("DELETE FROM paper_entries WHERE paper_entry_id=:id", entry_id),
        ("UPDATE papers SET scoring_plugin_id='changed' WHERE paper_id=:id", paper_id),
        (
            "UPDATE papers SET scoring_plugin_id='changed', scoring_plugin_version='v1' WHERE paper_id=:id",
            paper_id,
        ),
        ("UPDATE papers SET status='DRAFT' WHERE paper_id=:id", paper_id),
        ("DELETE FROM papers WHERE paper_id=:id", paper_id),
        ("INSERT INTO paper_entries VALUES (:newid, :id, :revision, 3)", paper_id),
    ]
    for sql, identifier in statements:
        with pytest.raises(DBAPIError), db.begin() as c:
            c.execute(
                text(sql), {"id": identifier, "revision": newer.task_revision_id, "newid": uuid4()}
            )
    with pytest.raises(ValueError, match="not a draft"), Session(db) as s, s.begin():
        remove_entry(s, paper_id=paper_id, expected_version=1, entry_id=entry_id)
    with Session(db) as s, s.begin():
        copied = copy_paper(s, source_id=paper_id)
        assert entries(s, copied.paper_id)[0].task_revision_id == task.task_revision_id
    with db.begin() as c:
        c.execute(text("UPDATE papers SET status='ARCHIVED' WHERE paper_id=:id"), {"id": paper_id})
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(
            text("UPDATE paper_entries SET position=3 WHERE paper_entry_id=:id"), {"id": entry_id}
        )


def test_duplicate_position_rejected_at_commit(db, bank):
    task = write(db, bank)
    paper_id = draft(db, bank)
    with pytest.raises(IntegrityError), Session(db) as s, s.begin():
        add_entry(s, paper_id=paper_id, expected_version=0, task_id=task.task_id, position=1)
        add_entry(s, paper_id=paper_id, expected_version=1, task_id=task.task_id, position=1)
    with Session(db) as s:
        assert entries(s, paper_id) == [] and s.get(Paper, paper_id).edit_version == 0


def test_two_concurrent_editors_detect_stale_version(db, bank):
    task = write(db, bank)
    paper_id = draft(db, bank)
    barrier = Barrier(3, timeout=10)

    def worker(position):
        barrier.wait()
        try:
            with Session(db) as s, s.begin():
                add_entry(
                    s,
                    paper_id=paper_id,
                    expected_version=0,
                    task_id=task.task_id,
                    position=position,
                )
            return "ok"
        except PaperEditConflict:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        with db.begin() as blocker:
            schema = blocker.scalar(text("SELECT current_schema()"))
            blocker.execute(
                select(Paper.paper_id).where(Paper.paper_id == paper_id).with_for_update()
            )
            futures = [pool.submit(worker, i) for i in (1, 2)]
            barrier.wait()
            deadline = time.monotonic() + 10
            with db.connect() as observer:
                while time.monotonic() < deadline:
                    blocked = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:schema AND cardinality(pg_blocking_pids(pid)) > 0"
                        ),
                        {"schema": schema},
                    )
                    if blocked >= 2:
                        break
                    observer.commit()
                    time.sleep(0.05)
                assert blocked >= 2
        results = [f.result(timeout=10) for f in futures]
    assert sorted(results) == ["ok", "stale"]
    with Session(db) as s:
        assert len(entries(s, paper_id)) == 1 and s.get(Paper, paper_id).edit_version == 1
