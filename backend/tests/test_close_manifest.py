"""Close transaction and shared lock boundary on real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_assessment_session import create
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG, TestPlugin
from test_submission import deliver, start, submit
from test_taskbank import CONTENT, write

from app.assessment_session import (
    AssessmentSession,
    Attempt,
    SessionTask,
    ordered_tasks,
    redo_attempt,
    start_attempt,
)
from app.close_manifest import (
    CloseManifest,
    CloseManifestEntry,
    IncompleteSession,
    SettlementIntent,
    close_session,
    manifest_entries,
)
from app.fixed_audio import AudioStore, prepare_audio
from app.paper import add_entry
from app.publication import publish, select_plugin
from app.scoring_registry import Registry
from app.submission import Submission

pytest_plugins = ("test_submission",)


@pytest.fixture
def two_tasks(db, bank, tmp_path):
    one = write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="A"),
        code="1",
    )
    two = write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="B"),
        code="2",
    )
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = ()
    reg = Registry([plugin], [])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        e1 = add_entry(
            s, paper_id=paper, expected_version=0, task_id=one.task_id, position=2
        ).paper_entry_id
        e2 = add_entry(
            s, paper_id=paper, expected_version=1, task_id=two.task_id, position=1
        ).paper_entry_id
        select_plugin(
            s, paper_id=paper, expected_version=2, plugin_id="test-only", version="1", registry=reg
        )
        publish(s, paper_id=paper, expected_version=3, registry=reg, audio_config=TEST_AUDIO_CONFIG)
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        sid = create(s, (paper, one, e1, e2, reg, store)).session_id
        tasks = ordered_tasks(s, sid)
        assert [t.paper_entry_id for t in tasks] == [e2, e1]
        contexts = []
        for task in tasks:
            aid = start_attempt(s, session_id=sid, session_task_id=task.session_task_id).attempt_id
            contexts.append(
                {"session_id": sid, "session_task_id": task.session_task_id, "attempt_id": aid}
            )
    return contexts


def close(s, ctx, request="close"):
    return close_session(s, session_id=ctx["session_id"], close_request_id=request)


def test_order_missing_immutable_and_retry(db, two_tasks):
    first, second = two_tasks
    deliver(db, first)
    with Session(db) as s, s.begin():
        first_submission = submit(s, first).submission_id
    with pytest.raises(IncompleteSession) as exc, Session(db) as s, s.begin():
        close(s, first)
    assert exc.value.task_ids == [second["session_task_id"]]
    with Session(db) as s:
        assert s.get(AssessmentSession, first["session_id"]).status == "IN_PROGRESS"
        assert s.scalar(select(func.count()).select_from(CloseManifest)) == 0
        assert s.scalar(select(func.count()).select_from(SettlementIntent)) == 0
    deliver(db, second)
    with Session(db) as s, s.begin():
        second_submission = submit(s, second).submission_id
        manifest_id = close(s, first).manifest_id
        entries = manifest_entries(s, manifest_id)
        assert [(e.session_task_id, e.submission_id) for e in entries] == [
            (first["session_task_id"], first_submission),
            (second["session_task_id"], second_submission),
        ]
        assert s.get(AssessmentSession, first["session_id"]).status == "INPUT_CLOSED"
        assert close(s, first).manifest_id == manifest_id
        assert submit(s, first).submission_id == first_submission
        assert s.scalar(select(func.count()).select_from(SettlementIntent)) == 1
    with pytest.raises(ValueError, match="another request"), Session(db) as s, s.begin():
        close(s, first, request="different")
    with pytest.raises(ValueError, match="conflicts"), Session(db) as s, s.begin():
        submit(s, first, choice="B")
    with pytest.raises(ValueError, match="not in progress"), Session(db) as s, s.begin():
        submit(s, first, request="new")
    with pytest.raises(ValueError, match="not in progress"), Session(db) as s, s.begin():
        start(s, first, "USER_REPLAY", "late")
    with pytest.raises(ValueError, match="not in progress"), Session(db) as s, s.begin():
        redo_attempt(
            s,
            session_id=first["session_id"],
            session_task_id=first["session_task_id"],
            redo_request_id="late",
        )
    for sql, params in (
        (
            "UPDATE close_manifests SET close_request_id='evil' WHERE manifest_id=:id",
            {"id": manifest_id},
        ),
        ("DELETE FROM close_manifest_entries WHERE manifest_id=:id", {"id": manifest_id}),
        ("UPDATE submissions SET inputs='{}' WHERE submission_id=:id", {"id": first_submission}),
        (
            "UPDATE assessment_sessions SET status='IN_PROGRESS' WHERE session_id=:id",
            {"id": first["session_id"]},
        ),
        (
            "UPDATE session_tasks SET current_attempt_id=NULL WHERE session_task_id=:id",
            {"id": first["session_task_id"]},
        ),
        (
            "UPDATE attempts SET status='ABANDONED' WHERE attempt_id=:id",
            {"id": first["attempt_id"]},
        ),
        (
            "INSERT INTO attempts (attempt_id,session_task_id,status) VALUES (:attempt,:task,'OPEN')",
            {"attempt": uuid4(), "task": first["session_task_id"]},
        ),
        (
            "INSERT INTO close_manifest_entries (manifest_id,session_id,session_task_id,submission_id) VALUES (:manifest,:session,:task,:submission)",
            {
                "manifest": manifest_id,
                "session": first["session_id"],
                "task": first["session_task_id"],
                "submission": first_submission,
            },
        ),
    ):
        with pytest.raises(DBAPIError), db.begin() as c:
            c.execute(text(sql), params)
    with Session(db) as s:
        assert [e.submission_id for e in manifest_entries(s, manifest_id)] == [
            first_submission,
            second_submission,
        ]


def test_redo_does_not_fallback_to_old_submission(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        old = submit(s, context).submission_id
        b = redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            redo_request_id="redo",
        ).attempt_id
    with pytest.raises(IncompleteSession) as exc, Session(db) as s, s.begin():
        close(s, context)
    assert exc.value.task_ids == [context["session_task_id"]]
    with Session(db) as s:
        assert s.get(Submission, old).attempt_id == context["attempt_id"]
        assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == b
        assert s.scalar(select(func.count()).select_from(CloseManifest)) == 0
        assert s.get(AssessmentSession, context["session_id"]).status == "IN_PROGRESS"
    replacement = dict(context, attempt_id=b)
    deliver(db, replacement)
    with Session(db) as s, s.begin():
        current = submit(s, replacement, request="replacement").submission_id
        manifest = close(s, replacement).manifest_id
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(text("UPDATE submissions SET inputs='{}' WHERE submission_id=:id"), {"id": old})
    with Session(db) as s:
        assert [entry.submission_id for entry in manifest_entries(s, manifest)] == [current]
        assert submit(s, context).submission_id == old
    with pytest.raises(ValueError, match="conflicts"), Session(db) as s, s.begin():
        submit(s, replacement)


def test_close_transaction_rollback(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        close(s, context)
        raise RuntimeError("interrupted")
    with Session(db) as s:
        assert s.get(AssessmentSession, context["session_id"]).status == "IN_PROGRESS"
        assert s.scalar(select(func.count()).select_from(CloseManifest)) == 0
        assert s.scalar(select(func.count()).select_from(CloseManifestEntry)) == 0
        assert s.scalar(select(func.count()).select_from(SettlementIntent)) == 0


def test_concurrent_close_is_idempotent(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
    barrier = Barrier(2)

    def work(_):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return close(s, context).manifest_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(work, range(2)))
    assert ids[0] == ids[1]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(CloseManifest)) == 1
        assert s.scalar(select(func.count()).select_from(SettlementIntent)) == 1


def test_concurrent_submit_and_close(db, context):
    deliver(db, context)
    barrier = Barrier(2)

    def send():
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return submit(s, context).submission_id

    def end():
        try:
            with Session(db) as s, s.begin():
                barrier.wait(timeout=10)
                return close(s, context).manifest_id
        except IncompleteSession:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(send), pool.submit(end)
        submission_id, manifest_id = a.result(timeout=10), b.result(timeout=10)
    with Session(db) as s:
        if manifest_id:
            assert [x.submission_id for x in manifest_entries(s, manifest_id)] == [submission_id]
        else:
            assert s.get(AssessmentSession, context["session_id"]).status == "IN_PROGRESS"
            assert s.scalar(select(func.count()).select_from(CloseManifest)) == 0


def test_concurrent_redo_and_close(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        old = submit(s, context).submission_id
    barrier = Barrier(2)

    def redo():
        try:
            with Session(db) as s, s.begin():
                barrier.wait(timeout=10)
                return redo_attempt(
                    s,
                    session_id=context["session_id"],
                    session_task_id=context["session_task_id"],
                    redo_request_id="redo",
                ).attempt_id
        except ValueError:
            return None

    def end():
        try:
            with Session(db) as s, s.begin():
                barrier.wait(timeout=10)
                return close(s, context).manifest_id
        except IncompleteSession:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(redo), pool.submit(end)
        new_id, manifest_id = a.result(timeout=10), b.result(timeout=10)
    assert (new_id is None) != (manifest_id is None)
    with Session(db) as s:
        if manifest_id:
            assert [e.submission_id for e in manifest_entries(s, manifest_id)] == [old]
            assert s.get(AssessmentSession, context["session_id"]).status == "INPUT_CLOSED"
        else:
            assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == new_id
            assert s.get(Attempt, new_id).status == "OPEN"
            assert s.get(AssessmentSession, context["session_id"]).status == "IN_PROGRESS"
            assert s.scalar(select(func.count()).select_from(CloseManifest)) == 0


def test_downgrade_preserves_close(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
        close(s, context)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0009_submission")
        c.rollback()
        assert c.scalar(text("SELECT count(*) FROM close_manifests")) == 1
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0010_close_manifest"


@pytest.mark.parametrize("first_action", ["submit", "redo"])
def test_close_waits_for_authoritative_session_lock(db, context, first_action):
    import time
    from threading import Event

    deliver(db, context)
    if first_action == "redo":
        with Session(db) as s, s.begin():
            submit(s, context)
    held, release, entering = Event(), Event(), Event()

    def first():
        with Session(db) as s, s.begin():
            if first_action == "submit":
                result = submit(s, context).submission_id
            else:
                result = redo_attempt(
                    s,
                    session_id=context["session_id"],
                    session_task_id=context["session_task_id"],
                    redo_request_id="redo",
                ).attempt_id
            held.set()
            assert release.wait(10)
            return result

    def end():
        assert held.wait(10)
        with Session(db) as s, s.begin():
            entering.set()
            return close(s, context).manifest_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(end)
        try:
            assert entering.wait(10)
            with db.connect() as c:
                schema = c.scalar(text("SELECT current_schema()"))
                deadline, blocked = time.monotonic() + 10, False
                while time.monotonic() < deadline:
                    blocked = c.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name=:s AND cardinality(pg_blocking_pids(pid)) > 0)"
                        ),
                        {"s": schema},
                    )
                    c.commit()
                    if blocked:
                        break
                    time.sleep(0.05)
                assert blocked
        finally:
            release.set()
        result = a.result(timeout=10)
        if first_action == "submit":
            manifest = b.result(timeout=10)
            with Session(db) as s:
                assert [entry.submission_id for entry in manifest_entries(s, manifest)] == [result]
        else:
            with pytest.raises(IncompleteSession):
                b.result(timeout=10)
            with Session(db) as s:
                assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == result
                assert s.get(AssessmentSession, context["session_id"]).status == "IN_PROGRESS"


def test_upgrade_refuses_legacy_closed_session(db, context):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0009_submission")
        c.commit()
        c.execute(
            text("UPDATE assessment_sessions SET status='INPUT_CLOSED' WHERE session_id=:id"),
            {"id": context["session_id"]},
        )
        c.commit()
        with pytest.raises(DBAPIError, match="reconciliation"):
            command.upgrade(config, "head")
        c.rollback()
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0009_submission"
        assert (
            c.scalar(
                text("SELECT status FROM assessment_sessions WHERE session_id=:id"),
                {"id": context["session_id"]},
            )
            == "INPUT_CLOSED"
        )


def test_empty_published_paper_is_not_given_a_new_item_minimum(db, bank, tmp_path):
    plugin = TestPlugin()
    reg = Registry([plugin], [])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        select_plugin(
            s, paper_id=paper, expected_version=0, plugin_id="test-only", version="1", registry=reg
        )
        publish(s, paper_id=paper, expected_version=1, registry=reg, audio_config=TEST_AUDIO_CONFIG)
        sid = create(s, (paper, None, None, None, reg, AudioStore(tmp_path))).session_id
        manifest = close_session(s, session_id=sid, close_request_id="close")
        assert manifest_entries(s, manifest.manifest_id) == []
        assert s.get(SettlementIntent, manifest.manifest_id) is not None
