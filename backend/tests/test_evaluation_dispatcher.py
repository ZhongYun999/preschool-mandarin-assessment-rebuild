"""Dispatcher and candidate protection against concurrent redo/closure."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_worker import (
    FakeAdapter,
    MissingAudioAdapter,
    claim,
    deliver,
    expire,
    prepare,
    reg,
    submit,
)

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    authorize_retry,
    execute_claim,
    run_once,
    schedule,
)
from app.sessions.assessment_session import redo_attempt
from app.sessions.close_manifest import close_session, manifest_entries
from app.sessions.submission import EvaluationIntent

pytest_plugins = ("test_evaluation_worker", "test_close_manifest")


def tick(db, adapter=None, batch=10):
    adapters = {} if adapter is None else {("fake", "1"): adapter}
    return run_once(
        db, registry=reg(), adapters=adapters, lease=timedelta(minutes=2), batch_size=batch
    )


def submit_context(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        return submit(s, context).submission_id


def test_scan_completes_and_restart_does_not_repeat(db, context):
    sid = submit_context(db, context)
    adapter = FakeAdapter()
    result = tick(db, adapter)
    assert result == {"scheduled": 1, "recovered": 0, "claimed": 1, "finished": 1}
    assert tick(db, adapter) == {"scheduled": 0, "recovered": 0, "claimed": 0, "finished": 0}
    assert adapter.calls == 1
    with Session(db) as s:
        job = s.scalar(select(EvaluationJob))
        assert job.status == "READY" and job.submission_id == sid
        assert job.requirement["source"] == "fake"
        assert s.scalar(select(RawEvaluation)).payload == {"metric": 0}


def test_no_adapter_or_missing_dependency_stays_waiting(db, context):
    submit_context(db, context)
    assert tick(db)["scheduled"] == 1
    adapter = MissingAudioAdapter()
    assert tick(db, adapter)["claimed"] == 0
    with Session(db) as s:
        job = s.scalar(select(EvaluationJob))
        assert job.status == "WAITING" and job.attempts == 0
    assert adapter.calls == 0
    ready = FakeAdapter()
    assert tick(db, ready)["finished"] == 1
    assert ready.calls == 1


def test_no_declared_requirements_generate_no_jobs_or_ise(db, two_tasks):
    for ctx in two_tasks:
        deliver(db, ctx)
        with Session(db) as s, s.begin():
            submit(s, ctx)
    adapter = FakeAdapter()
    assert tick(db, adapter)["scheduled"] == 0
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 0
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert adapter.calls == 0


def test_concurrent_dispatchers_do_not_duplicate_work(db, context):
    submit_context(db, context)
    barrier = Barrier(2)
    adapter = FakeAdapter()

    def work(_):
        barrier.wait(timeout=10)
        return tick(db, adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, range(2)))
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 1
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == 1
        assert s.scalar(select(EvaluationJob.status)) == "READY"
    assert adapter.calls == 1


def test_old_unstarted_job_is_skipped_and_never_rescanned(db, context):
    old, job_id = prepare(db, context)
    with Session(db) as s, s.begin():
        redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            expected_attempt_id=context["attempt_id"],
            redo_request_id="redo",
        )
    adapter = FakeAdapter()
    assert tick(db, adapter)["claimed"] == 0
    assert tick(db, adapter)["claimed"] == 0
    with Session(db) as s:
        job = s.get(EvaluationJob, job_id)
        assert job.status == "SKIPPED" and job.error_code == "SUPERSEDED"
        assert job.submission_id == old and job.attempts == 0
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == 0
    with pytest.raises(ValueError, match="cannot be retried"), Session(db) as s, s.begin():
        authorize_retry(s, job_id)
    assert adapter.calls == 0


def test_redo_after_claim_before_dispatch_is_skipped(db, context):
    _, job_id = prepare(db, context)
    adapter = FakeAdapter()
    work = claim(db, job_id, adapter)
    with Session(db) as s, s.begin():
        redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            expected_attempt_id=context["attempt_id"],
            redo_request_id="redo",
        )
    assert not execute_claim(db, work)
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "SKIPPED"
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == 0
    assert adapter.calls == 0


def test_redo_lock_serializes_before_claim(db, context):
    _, job_id = prepare(db, context)
    held, release, entering = Event(), Event(), Event()
    adapter = FakeAdapter()

    def redo():
        with Session(db) as s, s.begin():
            redo_attempt(
                s,
                session_id=context["session_id"],
                session_task_id=context["session_task_id"],
                expected_attempt_id=context["attempt_id"],
                redo_request_id="redo",
            )
            held.set()
            assert release.wait(10)

    def take():
        assert held.wait(10)
        entering.set()
        return claim(db, job_id, adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(redo), pool.submit(take)
        try:
            assert entering.wait(10)
            with db.connect() as c:
                schema = c.scalar(text("SELECT current_schema()"))
                until, blocked = time.monotonic() + 10, False
                while time.monotonic() < until:
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
        a.result(timeout=10)
        assert b.result(timeout=10) is None
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "SKIPPED"
    assert adapter.calls == 0


def test_closed_session_uses_manifest_candidate(db, context):
    old, old_job = prepare(db, context)
    with Session(db) as s, s.begin():
        aid = redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            expected_attempt_id=context["attempt_id"],
            redo_request_id="redo",
        ).attempt_id
    newer = dict(context, attempt_id=aid)
    deliver(db, newer)
    with Session(db) as s, s.begin():
        current = submit(s, newer, request="new").submission_id
        manifest = close_session(
            s, session_id=context["session_id"], close_request_id="close"
        ).manifest_id
    adapter = FakeAdapter()
    tick(db, adapter)
    tick(db, adapter)
    with Session(db) as s:
        assert [e.submission_id for e in manifest_entries(s, manifest)] == [current]
        assert s.get(EvaluationJob, old_job).status == "SKIPPED"
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.status == "READY"))
        assert raw.submission_id == current and raw.submission_id != old
    assert adapter.calls == 1


def test_unknown_remains_stopped_until_authorized(db, context):
    submit_context(db, context)
    failing = FakeAdapter("unknown")
    tick(db, failing)
    for _ in range(3):
        tick(db, failing)
    assert failing.calls == 1
    with Session(db) as s, s.begin():
        job = s.scalar(select(EvaluationJob))
        assert job.status == "UNKNOWN"
        authorize_retry(s, job.job_id, accept_unknown=True)
    success = FakeAdapter()
    assert tick(db, success)["finished"] == 1
    assert success.calls == 1
    with Session(db) as s:
        assert set(s.scalars(select(RawEvaluation.status))) == {"UNKNOWN", "READY"}


def test_undispatched_expired_claim_is_recovered_without_unknown(db, context):
    _, job_id = prepare(db, context)
    assert claim(db, job_id, FakeAdapter()) is not None
    expire(db, job_id)
    adapter = FakeAdapter()
    result = tick(db, adapter)
    assert result["recovered"] == 1 and result["finished"] == 1
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "READY"
        assert list(s.scalars(select(RawEvaluation.status))) == ["READY"]
    assert adapter.calls == 1


def test_downgrade_wont_discard_skipped_facts(db, context):
    _, job_id = prepare(db, context)
    with Session(db) as s, s.begin():
        redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            expected_attempt_id=context["attempt_id"],
            redo_request_id="redo",
        )
    tick(db)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0012_evaluation_jobs")
        c.rollback()
        assert (
            c.scalar(text("SELECT status FROM evaluation_jobs WHERE job_id=:id"), {"id": job_id})
            == "SKIPPED"
        )


def test_waiting_dependency_can_become_ready_from_persistent_fact(db, context):
    from app.evaluation.worker import InputSnapshot

    sid = submit_context(db, context)
    with db.begin() as c:
        c.execute(
            text(
                "CREATE TABLE test_dependency_facts (submission_id uuid PRIMARY KEY REFERENCES submissions(submission_id), value text NOT NULL)"
            )
        )

    class FactAdapter(FakeAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            fact = session.scalar(
                text("SELECT value FROM test_dependency_facts WHERE submission_id=:id"),
                {"id": submission.submission_id},
            )
            if fact is None:
                return None
            return InputSnapshot(
                refs={"fact_submission_id": str(submission.submission_id)}, payload={"choice": fact}
            )

    adapter = FactAdapter()
    assert tick(db, adapter)["claimed"] == 0
    assert adapter.calls == 0
    with db.begin() as c:
        c.execute(text("INSERT INTO test_dependency_facts VALUES (:id, 'A')"), {"id": sid})
    assert tick(db, adapter)["finished"] == 1
    assert adapter.calls == 1


def create_second_context(db, context, tmp_path):
    from test_publication import TestPlugin

    from app.media.fixed_audio import AudioStore
    from app.scoring.registry import EvaluationContract, Registry
    from app.sessions.assessment_session import (
        AssessmentSession,
        create_session,
        ordered_tasks,
        start_attempt,
    )

    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = (
        {"source": "fake", "version": "1", "parameters": {}},
    )
    registry = Registry(
        [plugin], [EvaluationContract("fake", "1", frozenset({"instruction"}), lambda p: p == {})]
    )
    with Session(db) as s, s.begin():
        paper_id = s.get(AssessmentSession, context["session_id"]).paper_id
        sid = create_session(
            s,
            paper_id=paper_id,
            scope="second",
            create_request_id="other",
            review_policy={"sampling": 0.25},
            store=AudioStore(tmp_path),
            registry=registry,
        ).session_id
        tid = ordered_tasks(s, sid)[0].session_task_id
        aid = start_attempt(s, session_id=sid, session_task_id=tid).attempt_id
    return {"session_id": sid, "session_task_id": tid, "attempt_id": aid}


def test_batch_rotation_prevents_unready_job_starvation(db, context, tmp_path):
    second = create_second_context(db, context, tmp_path)
    deliver(db, context)
    deliver(db, second)
    with Session(db) as s, s.begin():
        a = submit(s, context, choice="A").submission_id
        b = submit(s, second, choice="B").submission_id
        assert schedule(s) == 2
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE evaluation_jobs SET last_checked_at=clock_timestamp()-interval '1 hour' WHERE submission_id=:id"
            ),
            {"id": a},
        )

    class ConditionalAdapter(FakeAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            if submission.inputs["selected_choice_id"] == "A":
                return None
            return super().resolve_inputs(session, submission, revision, requirement)

    adapter = ConditionalAdapter()
    assert tick(db, adapter, batch=1)["claimed"] == 0
    assert tick(db, adapter, batch=1)["finished"] == 1
    with Session(db) as s:
        assert (
            s.scalar(select(EvaluationJob.status).where(EvaluationJob.submission_id == a))
            == "WAITING"
        )
        assert (
            s.scalar(select(EvaluationJob.status).where(EvaluationJob.submission_id == b))
            == "READY"
        )
    assert adapter.calls == 1


def test_dispatch_marker_blocks_duplicate_invocation(db, context):
    _, job_id = prepare(db, context)
    entered, release = Event(), Event()

    class BlockingAdapter(FakeAdapter):
        def evaluate(self, inputs, requirement):
            entered.set()
            assert release.wait(10)
            return super().evaluate(inputs, requirement)

    adapter = BlockingAdapter()
    work = claim(db, job_id, adapter)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, work)
        try:
            assert entered.wait(10)
            assert not execute_claim(db, work)
        finally:
            release.set()
        assert future.result(timeout=10)
    assert adapter.calls == 1


def test_claim_first_redo_waits_then_pre_dispatch_rechecks(db, context):
    _, job_id = prepare(db, context)
    held, release, redo_entering = Event(), Event(), Event()

    class BlockingResolve(FakeAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            held.set()
            assert release.wait(10)
            return super().resolve_inputs(session, submission, revision, requirement)

    adapter = BlockingResolve()

    def take():
        return claim(db, job_id, adapter)

    def redo():
        assert held.wait(10)
        with Session(db) as s, s.begin():
            redo_entering.set()
            return redo_attempt(
                s,
                session_id=context["session_id"],
                session_task_id=context["session_task_id"],
                expected_attempt_id=context["attempt_id"],
                redo_request_id="redo",
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(take), pool.submit(redo)
        try:
            assert redo_entering.wait(10)
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
        work = first.result(timeout=10)
        second.result(timeout=10)
    assert work is not None and not execute_claim(db, work)
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "SKIPPED"
    assert adapter.calls == 0


def test_upgrade_legacy_running_job_is_conservatively_unknown(db, context):
    from uuid import uuid4

    from app.evaluation.worker import recover_expired

    _, job_id = prepare(db, context)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0012_evaluation_jobs")
        c.commit()
        c.execute(
            text(
                "UPDATE evaluation_jobs SET status='RUNNING',claim_token=:token,lease_until=clock_timestamp()-interval '1 second',input_refs='{}'::jsonb,input_fingerprint='legacy' WHERE job_id=:id"
            ),
            {"id": job_id, "token": uuid4()},
        )
        c.commit()
        command.upgrade(config, "head")
        c.commit()
    with Session(db) as s, s.begin():
        assert s.get(EvaluationJob, job_id).dispatch_started_at is not None
        assert recover_expired(s) == 1
    adapter = FakeAdapter()
    assert tick(db, adapter)["claimed"] == 0
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "UNKNOWN"
        assert s.scalar(select(RawEvaluation.status)) == "UNKNOWN"
    assert adapter.calls == 0


def test_bounded_intent_queue_marks_only_materialized_jobs(db, context, tmp_path):
    second = create_second_context(db, context, tmp_path)
    submit_context(db, context)
    submit_context(db, second)
    assert tick(db, batch=1)["scheduled"] == 1
    with Session(db) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(EvaluationIntent)
                .where(EvaluationIntent.scheduled_at.is_(None))
            )
            == 1
        )
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 1
    assert tick(db, batch=1)["scheduled"] == 1
    assert tick(db, batch=1)["scheduled"] == 0
    with Session(db) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(EvaluationIntent)
                .where(EvaluationIntent.scheduled_at.is_(None))
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 2
        intent_id = s.scalar(select(EvaluationIntent.intent_id))
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(
            text("UPDATE evaluation_intents SET scheduled_at=NULL WHERE intent_id=:id"),
            {"id": intent_id},
        )


def test_unmaterialized_intent_cannot_be_marked_scheduled(db, context):
    submit_context(db, context)
    with Session(db) as s:
        intent_id = s.scalar(select(EvaluationIntent.intent_id))
    with pytest.raises(DBAPIError, match="atomically"), db.begin() as c:
        c.execute(
            text(
                "UPDATE evaluation_intents SET scheduled_at=clock_timestamp() WHERE intent_id=:id"
            ),
            {"id": intent_id},
        )
    with Session(db) as s:
        assert s.get(EvaluationIntent, intent_id).scheduled_at is None
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0


def test_upgrade_preserves_existing_ready_evidence_and_backfills_queue(db, context):
    from uuid import uuid4

    _, job_id = prepare(db, context)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0012_evaluation_jobs")
        c.commit()
        submission_id = c.scalar(
            text("SELECT submission_id FROM evaluation_jobs WHERE job_id=:id"), {"id": job_id}
        )
        token = uuid4()
        c.execute(
            text(
                "UPDATE evaluation_jobs SET status='RUNNING',claim_token=:token,lease_until=clock_timestamp()+interval '1 minute',input_refs='{}'::jsonb,input_fingerprint='legacy' WHERE job_id=:id"
            ),
            {"id": job_id, "token": token},
        )
        c.execute(
            text(
                "INSERT INTO raw_evaluations (evaluation_id, job_id, submission_id, execution_id, status, input_refs, input_fingerprint, payload) VALUES (:id, :job, :submission, :token, 'READY', '{}'::jsonb, 'legacy', jsonb_build_object('metric', 0))"
            ),
            {"id": uuid4(), "job": job_id, "submission": submission_id, "token": token},
        )
        c.execute(
            text(
                "UPDATE evaluation_jobs SET status='READY',claim_token=NULL,lease_until=NULL WHERE job_id=:id"
            ),
            {"id": job_id},
        )
        c.commit()
        command.upgrade(config, "head")
        c.commit()
    adapter = FakeAdapter()
    assert tick(db, adapter)["scheduled"] == 0
    assert adapter.calls == 0
    with Session(db) as s:
        job = s.get(EvaluationJob, job_id)
        assert job.status == "READY"
        assert s.get(EvaluationIntent, job.intent_id).scheduled_at is not None
        assert s.scalar(select(RawEvaluation.payload)) == {"metric": 0}


def test_job_cannot_be_created_without_marking_intent(db, context):
    from app.papers.paper import EvaluationPlan

    submission_id = submit_context(db, context)
    with pytest.raises(DBAPIError, match="atomically"), Session(db) as s, s.begin():
        intent = s.scalar(
            select(EvaluationIntent).where(EvaluationIntent.submission_id == submission_id)
        )
        plan = s.get(EvaluationPlan, intent.paper_entry_id)
        s.add(
            EvaluationJob(
                intent_id=intent.intent_id,
                submission_id=intent.submission_id,
                paper_entry_id=intent.paper_entry_id,
                requirement_index=intent.requirement_index,
                requirement=plan.requirements[intent.requirement_index],
                status="WAITING",
                attempts=0,
            )
        )
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0
        assert s.scalar(select(EvaluationIntent.scheduled_at)) is None
