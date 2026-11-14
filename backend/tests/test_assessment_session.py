"""PostgreSQL checks for session identity and candidate serialization."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_fixed_audio import Synth, published
from test_publication import registry, setup_paper
from test_taskbank import CONTENT, write

from app.assessment_session import (
    AssessmentSession,
    Attempt,
    SessionTask,
    create_session,
    ordered_tasks,
    redo_attempt,
    start_attempt,
)
from app.fixed_audio import AudioStore, FixedAudio, prepare_audio
from app.paper import PaperEntry
from app.scoring_registry import Registry


@pytest.fixture
def ready(db, bank, tmp_path):
    paper, revision, a, b, reg = published(db, bank)
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    return paper, revision, a, b, reg, store


def create(s, ready, request="create", scope="trusted-caller", policy=None):
    paper, _, _, _, reg, store = ready
    return create_session(
        s,
        paper_id=paper,
        scope=scope,
        create_request_id=request,
        review_policy={"sampling": 0.25} if policy is None else policy,
        store=store,
        registry=reg,
    )


def created(db, ready):
    with Session(db) as s, s.begin():
        instance = create(s, ready)
        task_ids = [task.session_task_id for task in ordered_tasks(s, instance.session_id)]
        return instance.session_id, task_ids


def test_creation_idempotency_order_history_and_conflict(db, bank, ready):
    paper, revision, a, b, _, _ = ready
    session_id, ids = created(db, ready)
    with Session(db) as s, s.begin():
        assert create(s, ready).session_id == session_id
        assert [t.paper_entry_id for t in ordered_tasks(s, session_id)] == [a, b]
        assert len(ids) == 2
    with pytest.raises(ValueError, match="conflicts"), Session(db) as s, s.begin():
        create(s, ready, policy={"sampling": 0.5})
    with pytest.raises(ValueError, match="conflicts"), Session(db) as s, s.begin():
        create_session(
            s,
            paper_id=uuid4(),
            scope="trusted-caller",
            create_request_id="create",
            review_policy={"sampling": 0.25},
            store=ready[5],
            registry=ready[4],
        )
    with pytest.raises(ValueError, match="nonempty"), Session(db) as s, s.begin():
        create(s, ready, request="invalid", policy={})
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(
            text("UPDATE assessment_sessions SET review_policy='{}'::jsonb WHERE session_id=:id"),
            {"id": session_id},
        )
    with db.begin() as c:
        c.execute(text("UPDATE papers SET status='ARCHIVED' WHERE paper_id=:id"), {"id": paper})
    write(db, bank, replace(CONTENT, prompt_text="revised"))
    with Session(db) as s, s.begin():
        assert create(s, ready).session_id == session_id
        assert s.get(PaperEntry, a).task_revision_id == revision.task_revision_id
        assert len(ordered_tasks(s, session_id)) == 2
    with pytest.raises(ValueError, match="not published"), Session(db) as s, s.begin():
        create(s, ready, request="new")


def test_creation_refuses_unready_or_unavailable(db, bank, ready):
    paper, _, _, _, reg, store = ready
    with pytest.raises(ValueError, match="plugin"), Session(db) as s, s.begin():
        create_session(
            s,
            paper_id=paper,
            scope="x",
            create_request_id="x",
            review_policy={"sampling": 0.25},
            store=store,
            registry=Registry(),
        )
    with pytest.raises(ValueError, match="contract"), Session(db) as s, s.begin():
        create_session(
            s,
            paper_id=paper,
            scope="x",
            create_request_id="x",
            review_policy={"sampling": 0.25},
            store=store,
            registry=registry(contracts=False),
        )
    draft, *_ = setup_paper(db, bank)
    with pytest.raises(ValueError, match="published"), Session(db) as s, s.begin():
        create_session(
            s,
            paper_id=draft,
            scope="x",
            create_request_id="x",
            review_policy={"sampling": 0.25},
            store=store,
            registry=reg,
        )
    with Session(db) as s:
        audio = s.scalars(select(FixedAudio)).first()
        path = store.root / audio.storage_ref
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="not ready"), Session(db) as s, s.begin():
        create(s, ready)
    path.unlink()
    with pytest.raises(ValueError, match="not ready"), Session(db) as s, s.begin():
        create(s, ready)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 0
        assert s.scalar(select(func.count()).select_from(SessionTask)) == 0


def test_creation_rolls_back_session_and_tasks_together(db, ready):
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        create(s, ready)
        raise RuntimeError("interrupt after creation")
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 0
        assert s.scalar(select(func.count()).select_from(SessionTask)) == 0


def test_pending_and_failed_audio_block_creation(db, bank, tmp_path):
    paper, _, _, _, reg = published(db, bank)
    store = AudioStore(tmp_path)

    def invoke():
        with Session(db) as s, s.begin():
            create_session(
                s,
                paper_id=paper,
                scope="x",
                create_request_id="x",
                review_policy={"sampling": 0.25},
                store=store,
                registry=reg,
            )

    with pytest.raises(ValueError, match="not ready"):
        invoke()
    synth = Synth()
    synth.fail = True
    prepare_audio(db, paper, store, synth)
    with pytest.raises(ValueError, match="not ready"):
        invoke()


def test_attempt_restore_redo_sealed_and_rollback(db, ready):
    session_id, (task_id, _) = created(db, ready)
    with Session(db) as s, s.begin():
        a = start_attempt(s, session_id=session_id, session_task_id=task_id).attempt_id
        assert start_attempt(s, session_id=session_id, session_task_id=task_id).attempt_id == a
    with Session(db) as s, s.begin():
        b = redo_attempt(
            s, session_id=session_id, session_task_id=task_id, redo_request_id="b"
        ).attempt_id
        assert s.get(Attempt, a).status == "ABANDONED"
        assert (
            redo_attempt(
                s, session_id=session_id, session_task_id=task_id, redo_request_id="b"
            ).attempt_id
            == b
        )
    # Test-only historical precondition; production cannot seal without Submission.
    with db.begin() as c:
        c.execute(text("UPDATE attempts SET status='SEALED' WHERE attempt_id=:id"), {"id": b})
    with Session(db) as s, s.begin():
        c_id = redo_attempt(
            s, session_id=session_id, session_task_id=task_id, redo_request_id="c"
        ).attempt_id
        assert s.get(Attempt, b).status == "SEALED"
        assert s.get(SessionTask, task_id).current_attempt_id == c_id
        assert s.get(Attempt, c_id).status == "OPEN"
        assert (
            redo_attempt(
                s, session_id=session_id, session_task_id=task_id, redo_request_id="b"
            ).attempt_id
            == b
        )
        assert s.get(SessionTask, task_id).current_attempt_id == c_id
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        redo_attempt(
            s, session_id=session_id, session_task_id=task_id, redo_request_id="rolled-back"
        )
        raise RuntimeError("interrupt")
    with Session(db) as s:
        assert s.get(SessionTask, task_id).current_attempt_id == c_id
        assert s.get(Attempt, c_id).status == "OPEN"
        assert s.scalar(select(func.count()).select_from(Attempt)) == 3
    with db.begin() as c:
        c.execute(
            text("UPDATE assessment_sessions SET status='INPUT_CLOSED' WHERE session_id=:id"),
            {"id": session_id},
        )
    for fn in (
        lambda s: start_attempt(s, session_id=session_id, session_task_id=task_id),
        lambda s: redo_attempt(
            s, session_id=session_id, session_task_id=task_id, redo_request_id="d"
        ),
    ):
        with pytest.raises(ValueError, match="not in progress"), Session(db) as s, s.begin():
            fn(s)


def test_composite_constraints_reject_cross_bindings(db, ready):
    first, (task1, task2) = created(db, ready)
    with Session(db) as s, s.begin():
        other = create(s, ready, request="other").session_id
        attempt = start_attempt(s, session_id=first, session_task_id=task1).attempt_id
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(
            text("UPDATE session_tasks SET current_attempt_id=:a WHERE session_task_id=:t"),
            {"a": attempt, "t": task2},
        )
    other_paper = uuid4()
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(
            text("UPDATE session_tasks SET paper_id=:p WHERE session_task_id=:t"),
            {"p": other_paper, "t": task1},
        )
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(
            text("UPDATE session_tasks SET session_id=:s WHERE session_task_id=:t"),
            {"s": other, "t": task1},
        )


def test_concurrent_create_and_redo(db, ready):
    barrier = Barrier(2)

    def do_create():
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return create(s, ready).session_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: do_create(), range(2)))
    assert ids[0] == ids[1]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 1
        task_id = ordered_tasks(s, ids[0])[0].session_task_id
    barrier = Barrier(2)

    def redo(key):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return redo_attempt(
                s, session_id=ids[0], session_task_id=task_id, redo_request_id=key
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redo, ["one", "two"]))
    with Session(db) as s:
        attempts = s.scalars(select(Attempt).where(Attempt.session_task_id == task_id)).all()
        assert len(attempts) == 2
        assert {a.status for a in attempts} == {"OPEN", "ABANDONED"}
        current = s.get(SessionTask, task_id).current_attempt_id
        assert current in results and s.get(Attempt, current).status == "OPEN"


def test_downgrade_rejects_existing_data(db, ready):
    created(db, ready)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0007_tts_attempts")
        c.rollback()
        assert c.scalar(text("SELECT count(*) FROM assessment_sessions")) == 1
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version")) == "0008_session_foundation"
        )


def test_concurrent_start_and_same_redo_are_idempotent(db, ready):
    session_id, (task_id, _) = created(db, ready)
    barrier = Barrier(2)

    def start(_):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return start_attempt(s, session_id=session_id, session_task_id=task_id).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, range(2)))
    assert results[0] == results[1]
    barrier = Barrier(2)

    def redo(_):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return redo_attempt(
                s, session_id=session_id, session_task_id=task_id, redo_request_id="same"
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redo, range(2)))
    assert results[0] == results[1]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(Attempt)) == 2
        assert s.get(SessionTask, task_id).current_attempt_id == results[0]


def test_creation_serializes_with_paper_archive(db, ready):
    from threading import Event

    locked, release, attempting = Event(), Event(), Event()
    paper_id = ready[0]

    def archive():
        with db.begin() as connection:
            connection.execute(
                text("SELECT paper_id FROM papers WHERE paper_id=:id FOR UPDATE"), {"id": paper_id}
            )
            locked.set()
            assert release.wait(10)
            connection.execute(
                text("UPDATE papers SET status='ARCHIVED' WHERE paper_id=:id"), {"id": paper_id}
            )

    def create_after_lock():
        assert locked.wait(10)
        with Session(db) as s, s.begin():
            attempting.set()
            return create(s, ready).session_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_future = pool.submit(archive)
        creation_future = pool.submit(create_after_lock)
        try:
            assert attempting.wait(10)
            with db.connect() as observer:
                schema = observer.scalar(text("SELECT current_schema()"))
                deadline = time.monotonic() + 10
                blocked = False
                while time.monotonic() < deadline:
                    blocked = observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE "
                            "application_name=:schema AND cardinality(pg_blocking_pids(pid)) > 0)"
                        ),
                        {"schema": schema},
                    )
                    observer.commit()
                    if blocked:
                        break
                    time.sleep(0.05)
                assert blocked
        finally:
            release.set()
        archive_future.result(timeout=10)
        with pytest.raises(ValueError, match="not published"):
            creation_future.result(timeout=10)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 0
