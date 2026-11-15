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
from test_taskbank import CONTENT, write

from app.assessment_session import Attempt, SessionTask, ordered_tasks, redo_attempt, start_attempt
from app.fixed_audio import AudioStore, prepare_audio
from app.paper import add_entry
from app.publication import publish, select_plugin
from app.scoring_registry import EvaluationContract, Registry
from app.submission import (
    EvaluationIntent,
    Playback,
    Submission,
    finish_playback,
    start_playback,
    submit_instruction,
)


@pytest.fixture
def context(db, bank, tmp_path):
    revision = write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="A"),
    )
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = (
        {"source": "fake", "version": "1", "parameters": {}},
    )
    reg = Registry(
        [plugin], [EvaluationContract("fake", "1", frozenset({"instruction"}), lambda p: p == {})]
    )
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        entry = add_entry(
            s, paper_id=paper_id, expected_version=0, task_id=revision.task_id, position=1
        )
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=1,
            plugin_id="test-only",
            version="1",
            registry=reg,
        )
        publish(
            s, paper_id=paper_id, expected_version=2, registry=reg, audio_config=TEST_AUDIO_CONFIG
        )
        entry_id = entry.paper_entry_id
    store = AudioStore(tmp_path)
    prepare_audio(db, paper_id, store, Synth())
    ready = (paper_id, revision, entry_id, None, reg, store)
    with Session(db) as s, s.begin():
        session_id = create(s, ready).session_id
        task_id = ordered_tasks(s, session_id)[0].session_task_id
        attempt_id = start_attempt(s, session_id=session_id, session_task_id=task_id).attempt_id
    return {"session_id": session_id, "session_task_id": task_id, "attempt_id": attempt_id}


def start(s, ctx, kind, request, retry=None):
    return start_playback(s, **ctx, kind=kind, request_id=request, retry_of_id=retry)


def finish(s, ctx, playback, completed=True):
    return finish_playback(s, **ctx, playback_id=playback, completed=completed)


def submit(s, ctx, request="submit", choice="A"):
    return submit_instruction(s, **ctx, submit_request_id=request, selected_choice_id=choice)


def deliver(db, ctx):
    with Session(db) as s, s.begin():
        a = start(s, ctx, "GUIDE", "g").playback_id
        finish(s, ctx, a)
        b = start(s, ctx, "INITIAL_PROMPT", "p").playback_id
        finish(s, ctx, b)


def test_playback_submit_and_replay_facts(db, context):
    with pytest.raises(ValueError, match="not ready"), Session(db) as s, s.begin():
        submit(s, context)
    with pytest.raises(ValueError, match="stage"), Session(db) as s, s.begin():
        start(s, context, "INITIAL_PROMPT", "early")
    with Session(db) as s, s.begin():
        guide = start(s, context, "GUIDE", "g").playback_id
        assert start(s, context, "GUIDE", "g").playback_id == guide
        finish(s, context, guide)
        initial = start(s, context, "INITIAL_PROMPT", "p").playback_id
        finish(s, context, initial, False)
        retry = start(s, context, "TECHNICAL_RETRY", "recover", initial).playback_id
        finish(s, context, retry)
        replay = start(s, context, "USER_REPLAY", "r").playback_id
    with pytest.raises(ValueError, match="not ready"), Session(db) as s, s.begin():
        submit(s, context)
    with Session(db) as s, s.begin():
        finish(s, context, replay)
        finish(s, context, replay)
        interrupted = start(s, context, "USER_REPLAY", "interrupted").playback_id
        finish(s, context, interrupted, False)
        result = submit(s, context, choice="B")
        submission_id = result.submission_id
        assert result.inputs == {"selected_choice_id": "B", "replay_playback_ids": [str(replay)]}
        assert s.get(Attempt, context["attempt_id"]).status == "SEALED"
        assert submit(s, context, choice="B").submission_id == submission_id
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 1
    with pytest.raises(ValueError, match="conflicts"), Session(db) as s, s.begin():
        submit(s, context, choice="A")
    with pytest.raises(ValueError, match="current and open"), Session(db) as s, s.begin():
        finish(s, context, interrupted)
    with Session(db) as s:
        assert s.get(Playback, initial).status == "FAILED"
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(
            text("UPDATE submissions SET inputs='{}' WHERE submission_id=:id"),
            {"id": submission_id},
        )


def test_redo_history_and_rollback(db, context):
    deliver(db, context)
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        submit(s, context)
        raise RuntimeError("interrupt")
    with Session(db) as s:
        assert s.get(Attempt, context["attempt_id"]).status == "OPEN"
        assert s.scalar(select(func.count()).select_from(Submission)) == 0
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 0
    with Session(db) as s, s.begin():
        old = submit(s, context).submission_id
        new = redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            redo_request_id="redo",
        ).attempt_id
    with Session(db) as s:
        assert s.get(Submission, old).attempt_id == context["attempt_id"]
        assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == new
        assert s.get(Attempt, new).status == "OPEN"
        assert s.scalar(select(Submission).where(Submission.attempt_id == new)) is None
    with pytest.raises(ValueError, match="current and open"), Session(db) as s, s.begin():
        start(s, context, "USER_REPLAY", "late")


def test_database_forbids_sealed_without_submission(db, context):
    with pytest.raises(DBAPIError, match="must coexist"), db.begin() as c:
        c.execute(
            text("UPDATE attempts SET status='SEALED' WHERE attempt_id=:id"),
            {"id": context["attempt_id"]},
        )


def test_concurrent_submit_redo(db, context):
    deliver(db, context)
    barrier = Barrier(2)

    def do_submit():
        try:
            with Session(db) as s, s.begin():
                barrier.wait(timeout=10)
                return submit(s, context).submission_id
        except ValueError:
            return None

    def do_redo():
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return redo_attempt(
                s,
                session_id=context["session_id"],
                session_task_id=context["session_task_id"],
                redo_request_id="redo",
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(do_submit), pool.submit(do_redo)
        submission_id, new = f1.result(timeout=10), f2.result(timeout=10)
    with Session(db) as s:
        assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == new
        assert s.get(Attempt, new).status == "OPEN"
        assert s.scalar(select(Submission).where(Submission.attempt_id == new)) is None
        assert s.get(Attempt, context["attempt_id"]).status == (
            "SEALED" if submission_id else "ABANDONED"
        )


def test_closed_session_refuses_playback_submit_and_redo(db, context):
    deliver(db, context)
    with db.begin() as c:
        c.execute(
            text("UPDATE assessment_sessions SET status='INPUT_CLOSED' WHERE session_id=:id"),
            {"id": context["session_id"]},
        )
    for operation in (
        lambda s: start(s, context, "USER_REPLAY", "r"),
        lambda s: submit(s, context),
        lambda s: redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            redo_request_id="x",
        ),
    ):
        with pytest.raises(ValueError, match="not in progress"), Session(db) as s, s.begin():
            operation(s)


def test_downgrade_preserves_facts(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0008_session_foundation")
        c.rollback()
        assert c.scalar(text("SELECT count(*) FROM submissions")) == 1


def test_invalid_choice_and_other_attempt_are_rejected(db, context):
    deliver(db, context)
    with pytest.raises(ValueError, match="Invalid instruction"), Session(db) as s, s.begin():
        submit(s, context, choice="D")
    wrong = dict(context, attempt_id=uuid4())
    with pytest.raises(ValueError, match="does not belong"), Session(db) as s, s.begin():
        submit(s, wrong)
    with Session(db) as s:
        assert s.get(Attempt, context["attempt_id"]).status == "OPEN"
        assert s.scalar(select(func.count()).select_from(Submission)) == 0


def test_concurrent_duplicate_submissions_have_one_intent(db, context):
    deliver(db, context)
    barrier = Barrier(2)

    def run(_):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return submit(s, context).submission_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(run, range(2)))
    assert ids[0] == ids[1]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(Submission)) == 1
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 1


def test_technical_retry_is_not_replay(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        replay = start(s, context, "USER_REPLAY", "r").playback_id
        finish(s, context, replay, False)
        retry = start(s, context, "TECHNICAL_RETRY", "retry", replay).playback_id
        finish(s, context, retry)
        result = submit(s, context)
        assert result.inputs["replay_playback_ids"] == []


def test_no_guide_and_no_evaluation_requirements(db, bank, tmp_path):
    revision = write(
        db, bank, replace(CONTENT, task_type="instruction", guide_text=None, reference_answer="C")
    )
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = ()
    reg = Registry([plugin], [])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        entry = add_entry(
            s, paper_id=paper, expected_version=0, task_id=revision.task_id, position=1
        ).paper_entry_id
        select_plugin(
            s, paper_id=paper, expected_version=1, plugin_id="test-only", version="1", registry=reg
        )
        publish(s, paper_id=paper, expected_version=2, registry=reg, audio_config=TEST_AUDIO_CONFIG)
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        sid = create(s, (paper, revision, entry, None, reg, store)).session_id
        tid = ordered_tasks(s, sid)[0].session_task_id
        aid = start_attempt(s, session_id=sid, session_task_id=tid).attempt_id
        ctx = {"session_id": sid, "session_task_id": tid, "attempt_id": aid}
        prompt = start(s, ctx, "INITIAL_PROMPT", "p").playback_id
        finish(s, ctx, prompt)
        submit(s, ctx, choice="C")
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 0


def test_upgrade_rejects_legacy_sealed_attempt_without_submission(db, context):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0008_session_foundation")
        c.commit()
        c.execute(
            text("UPDATE attempts SET status='SEALED' WHERE attempt_id=:id"),
            {"id": context["attempt_id"]},
        )
        c.commit()
        with pytest.raises(DBAPIError, match="reconciliation"):
            command.upgrade(config, "head")
        c.rollback()
        assert (
            c.scalar(text("SELECT version_num FROM alembic_version")) == "0008_session_foundation"
        )
        assert (
            c.scalar(
                text("SELECT status FROM attempts WHERE attempt_id=:id"),
                {"id": context["attempt_id"]},
            )
            == "SEALED"
        )


def test_other_task_type_cannot_submit_as_instruction(db, bank, tmp_path):
    from test_fixed_audio import published

    paper, revision, a, b, reg = published(db, bank)
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        sid = create(s, (paper, revision, a, b, reg, store)).session_id
        tid = ordered_tasks(s, sid)[0].session_task_id
        aid = start_attempt(s, session_id=sid, session_task_id=tid).attempt_id
    ctx = {"session_id": sid, "session_task_id": tid, "attempt_id": aid}
    with pytest.raises(ValueError, match="Invalid instruction"), Session(db) as s, s.begin():
        submit(s, ctx)
