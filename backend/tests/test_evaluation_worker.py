"""Evaluation scheduling and worker recovery on PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event

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
from test_submission import deliver, submit
from test_taskbank import CONTENT, write

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    authorize_retry,
    claim_job,
    execute_claim,
    recover_expired,
    schedule,
)
from app.media.fixed_audio import AudioStore, prepare_audio
from app.papers.paper import EvaluationPlan, add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.registry import EvaluationContract, Registry
from app.sessions.assessment_session import ordered_tasks, redo_attempt, start_attempt
from app.sessions.close_manifest import close_session, manifest_entries
from app.sessions.submission import EvaluationIntent, Submission

pytest_plugins = ("test_submission",)


class FakeAdapter:
    def __init__(self, mode="success"):
        self.mode, self.calls = mode, 0

    def resolve_inputs(self, session, submission, revision, requirement):
        return InputSnapshot(refs={}, payload={"choice": submission.inputs["selected_choice_id"]})

    def evaluate(self, inputs, requirement):
        self.calls += 1
        if self.mode == "failure":
            raise KnownFailure("TEMPORARY_FAILURE")
        if self.mode == "unknown":
            raise RuntimeError("private exception not persisted")
        if self.mode == "crash":
            raise SystemExit("simulated process interruption")
        return AdapterResult(
            payload={"metric": 0 if self.mode == "success" else "invalid"},
            provider="test-provider",
            model="test-model",
            engine_version="test-v1",
        )

    def validate_result(self, result, requirement):
        return type(result.payload.get("metric")) is int


class MissingAudioAdapter(FakeAdapter):
    def resolve_inputs(self, session, submission, revision, requirement):
        # No audio submission implementation exists yet; absence remains waiting.
        if "audio_id" not in submission.inputs:
            return
        raise AssertionError("Unexpected audio fixture")


def reg():
    return Registry(
        [], [EvaluationContract("fake", "1", frozenset({"instruction"}), lambda p: p == {})]
    )


def prepare(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submission_id = submit(s, context).submission_id
        assert schedule(s) == 1
    with Session(db) as s:
        return submission_id, s.scalar(select(EvaluationJob.job_id))


def claim(db, job_id, adapter, registry=None):
    return claim_job(
        db,
        job_id,
        registry=registry or reg(),
        adapters={("fake", "1"): adapter},
        lease=timedelta(minutes=2),
    )


def test_frozen_plan_idempotency_and_zero_value(db, context):
    submission_id, job_id = prepare(db, context)
    with Session(db) as s, s.begin():
        assert schedule(s) == 0
        job = s.get(EvaluationJob, job_id)
        intent = s.get(EvaluationIntent, job.intent_id)
        plan = s.get(EvaluationPlan, job.paper_entry_id)
        assert job.requirement == plan.requirements[intent.requirement_index]
        assert job.submission_id == submission_id == intent.submission_id
        assert job.requirement["source"] == "fake"
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 1
    adapter = FakeAdapter()
    work = claim(db, job_id, adapter)
    assert work and execute_claim(db, work)
    with Session(db) as s:
        job = s.get(EvaluationJob, job_id)
        raw = s.scalar(select(RawEvaluation))
        assert job.status == "READY"
        assert raw.status == "READY" and raw.payload == {"metric": 0}
        assert raw.submission_id == submission_id and raw.job_id == job_id
        assert raw.input_refs["submission_id"] == str(submission_id)
        assert (raw.provider, raw.model, raw.engine_version) == (
            "test-provider",
            "test-model",
            "test-v1",
        )
        assert raw.input_fingerprint == job.input_fingerprint
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(text("UPDATE raw_evaluations SET payload='{}' WHERE job_id=:id"), {"id": job_id})
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(
            text("UPDATE evaluation_jobs SET status='WAITING' WHERE job_id=:id"), {"id": job_id}
        )
    assert claim(db, job_id, adapter) is None
    assert adapter.calls == 1


def test_dependencies_and_unavailable_contract_are_not_claimed(db, context):
    _, job_id = prepare(db, context)
    adapter = MissingAudioAdapter()
    assert claim(db, job_id, adapter) is None
    assert claim(db, job_id, FakeAdapter(), registry=Registry()) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, job_id)
        assert job.status == "WAITING" and job.attempts == 0 and job.input_refs is None
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == 0
    assert adapter.calls == 0


def test_no_requirement_does_not_create_job(db, bank, tmp_path):
    task = write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="A"),
    )
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = ()
    registry = Registry([plugin], [])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        entry = add_entry(
            s, paper_id=paper, expected_version=0, task_id=task.task_id, position=1
        ).paper_entry_id
        select_plugin(
            s,
            paper_id=paper,
            expected_version=1,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        publish(
            s, paper_id=paper, expected_version=2, registry=registry, audio_config=TEST_AUDIO_CONFIG
        )
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        sid = create(s, (paper, task, entry, None, registry, store)).session_id
        tid = ordered_tasks(s, sid)[0].session_task_id
        aid = start_attempt(s, session_id=sid, session_task_id=tid).attempt_id
    ctx = {"session_id": sid, "session_task_id": tid, "attempt_id": aid}
    deliver(db, ctx)
    with Session(db) as s, s.begin():
        submit(s, ctx)
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 0
        assert schedule(s) == 0
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0


def test_concurrent_claim_and_schedule_are_unique(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
    barrier = Barrier(2)

    def scan(_):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            return schedule(s)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(scan, range(2))) == [0, 1]
    with Session(db) as s:
        job_id = s.scalar(select(EvaluationJob.job_id))
    barrier = Barrier(2)
    adapter = FakeAdapter()

    def take(_):
        barrier.wait(timeout=10)
        return claim(db, job_id, adapter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(take, range(2)))
    winners = [work for work in claims if work is not None]
    assert len(winners) == 1
    assert execute_claim(db, winners[0]) and adapter.calls == 1


@pytest.mark.parametrize("mode,status", [("failure", "FAILED"), ("invalid", "INVALID")])
def test_failure_and_invalid_are_distinct_from_zero_and_can_retry(db, context, mode, status):
    _, job_id = prepare(db, context)
    work = claim(db, job_id, FakeAdapter(mode))
    assert execute_claim(db, work)
    with Session(db) as s:
        raw = s.scalar(select(RawEvaluation))
        assert raw.status == status and raw.payload is None and raw.error_code
        if status == "INVALID":
            assert (raw.provider, raw.model, raw.engine_version) == (
                "test-provider",
                "test-model",
                "test-v1",
            )
        assert s.get(EvaluationJob, job_id).status == status
    with Session(db) as s, s.begin():
        authorize_retry(s, job_id)
    assert execute_claim(db, claim(db, job_id, FakeAdapter()))
    with Session(db) as s:
        assert [
            r.status
            for r in s.scalars(
                select(RawEvaluation).order_by(RawEvaluation.created_at, RawEvaluation.status)
            )
        ] == [status, "READY"]
        assert s.get(EvaluationJob, job_id).attempts == 2


def expire(db, job_id):
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE evaluation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:id"
            ),
            {"id": job_id},
        )


def test_crash_requires_explicit_unknown_retry_and_stale_execution_cannot_write(db, context):
    _, job_id = prepare(db, context)
    crashed = claim(db, job_id, FakeAdapter("crash"))
    with pytest.raises(SystemExit):
        execute_claim(db, crashed)
    expire(db, job_id)
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    with pytest.raises(ValueError, match="acknowledgment"), Session(db) as s, s.begin():
        authorize_retry(s, job_id)
    with Session(db) as s, s.begin():
        authorize_retry(s, job_id, accept_unknown=True)
    replacement = claim(db, job_id, FakeAdapter())
    assert execute_claim(db, replacement)
    assert not execute_claim(db, crashed)
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "READY"
        assert set(s.scalars(select(RawEvaluation.status))) == {"UNKNOWN", "READY"}


def test_late_result_cannot_overwrite_new_lease(db, context):
    _, job_id = prepare(db, context)
    entered, release = Event(), Event()

    class BlockingAdapter(FakeAdapter):
        def evaluate(self, inputs, requirement):
            entered.set()
            assert release.wait(10)
            return AdapterResult(payload={"metric": 10})

    old = claim(db, job_id, BlockingAdapter())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, old)
        try:
            assert entered.wait(10)
            expire(db, job_id)
            with Session(db) as s, s.begin():
                recover_expired(s)
                authorize_retry(s, job_id, accept_unknown=True)
            new = claim(db, job_id, FakeAdapter())
            assert execute_claim(db, new)
        finally:
            release.set()
        assert not future.result(timeout=10)
    with Session(db) as s:
        assert s.scalar(select(RawEvaluation).where(RawEvaluation.status == "READY")).payload == {
            "metric": 0
        }
        assert set(s.scalars(select(RawEvaluation.status))) == {"UNKNOWN", "READY"}


def test_schedule_rollback_and_unknown_failure(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        assert schedule(s) == 1
        raise RuntimeError("rollback")
    with Session(db) as s, s.begin():
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0
        assert schedule(s) == 1
        job_id = s.scalar(select(EvaluationJob.job_id))
    assert execute_claim(db, claim(db, job_id, FakeAdapter("unknown")))
    with Session(db) as s:
        job = s.get(EvaluationJob, job_id)
        assert job.status == "UNKNOWN" and job.error_code == "EXTERNAL_OUTCOME_UNKNOWN"
        raw = s.scalar(select(RawEvaluation))
        assert raw.status == "UNKNOWN" and raw.payload is None
        assert raw.error_code == "EXTERNAL_OUTCOME_UNKNOWN"


def test_old_results_stay_with_original_submission_after_redo_and_close(db, context):
    old_submission, old_job = prepare(db, context)
    entered, release = Event(), Event()

    class StartedAdapter(FakeAdapter):
        def evaluate(self, inputs, requirement):
            entered.set()
            assert release.wait(10)
            return super().evaluate(inputs, requirement)

    # The old call has already crossed the dispatch boundary before redo.
    old_claim = claim(db, old_job, StartedAdapter())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, old_claim)
        try:
            assert entered.wait(10)
            with Session(db) as s, s.begin():
                new_attempt = redo_attempt(
                    s,
                    session_id=context["session_id"],
                    session_task_id=context["session_task_id"],
                    redo_request_id="redo",
                ).attempt_id
            newer = dict(context, attempt_id=new_attempt)
            deliver(db, newer)
            with Session(db) as s, s.begin():
                new_submission = submit(s, newer, request="new").submission_id
                assert schedule(s) == 1
                manifest_id = close_session(
                    s, session_id=context["session_id"], close_request_id="close"
                ).manifest_id
                new_job = s.scalar(
                    select(EvaluationJob.job_id).where(
                        EvaluationJob.submission_id == new_submission
                    )
                )
            assert execute_claim(db, claim(db, new_job, FakeAdapter()))
        finally:
            release.set()
        assert future.result(timeout=10)
    with Session(db) as s:
        assert [e.submission_id for e in manifest_entries(s, manifest_id)] == [new_submission]
        assert (
            s.scalar(select(RawEvaluation.submission_id).where(RawEvaluation.job_id == old_job))
            == old_submission
        )
        assert (
            s.scalar(select(RawEvaluation.submission_id).where(RawEvaluation.job_id == new_job))
            == new_submission
        )
        assert s.get(Submission, old_submission).attempt_id == context["attempt_id"]


def test_downgrade_refuses_existing_evidence(db, context):
    _, job_id = prepare(db, context)
    assert execute_claim(db, claim(db, job_id, FakeAdapter()))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0011_session_task_identity")
        c.rollback()
        assert c.scalar(text("SELECT count(*) FROM raw_evaluations")) == 1
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0017_credential_revocation"


def test_retry_rejects_changed_resolved_input_identity(db, context):
    _, job_id = prepare(db, context)
    assert execute_claim(db, claim(db, job_id, FakeAdapter("failure")))
    with Session(db) as s, s.begin():
        authorize_retry(s, job_id)

    class ChangedInput(FakeAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            return InputSnapshot(refs={}, payload={"choice": "different"})

    adapter = ChangedInput()
    with pytest.raises(ValueError, match="inputs changed"):
        claim(db, job_id, adapter)
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "WAITING"
        assert s.get(EvaluationJob, job_id).attempts == 1
    assert adapter.calls == 0


def test_bad_intent_cannot_schedule_undeclared_source(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submission_id = submit(s, context).submission_id
        original = s.scalar(
            select(EvaluationIntent).where(EvaluationIntent.submission_id == submission_id)
        )
        s.add(
            EvaluationIntent(
                submission_id=submission_id,
                paper_entry_id=original.paper_entry_id,
                requirement_index=1,
            )
        )
    with pytest.raises(ValueError, match="frozen plan"), Session(db) as s, s.begin():
        schedule(s)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0


def test_changed_claim_cannot_be_sent_under_frozen_identity(db, context):
    _, job_id = prepare(db, context)
    adapter = FakeAdapter()
    work = claim(db, job_id, adapter)
    work.inputs.payload["choice"] = "B"
    with pytest.raises(ValueError, match="frozen input identity"):
        execute_claim(db, work)
    assert adapter.calls == 0
    with Session(db) as s:
        assert s.get(EvaluationJob, job_id).status == "RUNNING"
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == 0
