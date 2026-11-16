"""Frozen ASR dependencies, evidence identity and QA evaluation recovery."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_assessment_http import access, create_body, issue
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG, TestPlugin
from test_qa_http import deliver, record, submit, upload, wav
from test_taskbank import CONTENT, write
from test_teacher_api import AUTH

from app.api.assessment_repetition import get_recording_store
from app.api.teacher_api import get_audio_store
from app.api.teacher_review import get_scoring_registry
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    AdapterResult,
    InputIdentityConflict,
    InputSnapshot,
    KnownFailure,
    authorize_retry,
    claim_job,
    execute_claim,
    recording_input,
    run_once,
    schedule,
)
from app.main import app
from app.media.fixed_audio import AudioStore, prepare_audio
from app.media.recording_store import RecordingStore
from app.papers.paper import add_entry
from app.papers.publication import preflight_publication, publish, select_plugin
from app.scoring.models import FinalScoreResult
from app.scoring.registry import EvaluationContract, Registry
from app.scoring.worker import run_settlement_once

pytest_plugins = ("test_teacher_api",)
ASR = {"source": "test-asr", "version": "1", "parameters": {}}
ACOUSTIC = {"source": "test-acoustic", "version": "1", "parameters": {}}
LLM = {
    "source": "test-llm",
    "version": "1",
    "parameters": {},
    "depends_on": {"requirement_index": 2, "source": "test-asr", "version": "1"},
}


class Plugin(TestPlugin):
    def __init__(self, requirements):
        super().__init__()
        self.evaluation_requirements["qa"] = requirements
        self.calls = []

    def score_task(self, task_context, raw_evaluations, submission_facts):
        self.calls.append("task")
        return {"score": len(raw_evaluations)}

    def score_paper(self, paper_context, task_scores):
        self.calls.append("paper")
        return {"total": sum(x["score"]["score"] for x in task_scores)}


def make_registry(requirements, allow_no_text=False, version2=False):
    plugin = Plugin(requirements)
    contracts = [
        EvaluationContract(
            "test-asr",
            "1",
            frozenset({"qa"}),
            lambda p: p == {},
            False,
            recognition_result=lambda payload: {
                "status": payload["status"],
                "text": payload["text"],
            },
        ),
        EvaluationContract(
            "test-llm",
            "2" if allow_no_text else "1",
            frozenset({"qa"}),
            lambda p: p == {},
            False,
            accepts_no_text=allow_no_text,
        ),
        EvaluationContract("test-acoustic", "1", frozenset({"qa"}), lambda p: p == {}, False),
    ]
    if version2:
        contracts.append(
            EvaluationContract(
                "test-asr",
                "2",
                frozenset({"qa"}),
                lambda p: p == {},
                False,
                recognition_result=lambda payload: {
                    "status": payload["status"],
                    "text": payload["text"],
                },
            )
        )
    return plugin, Registry([plugin], contracts)


@pytest.fixture
def dependency_flow(db, bank, client, tmp_path):
    def create(
        *, no_text=False, allow_no_text=False, close=True, requirements=None, version2=False
    ):
        if requirements is None:
            llm = {**LLM, "version": "2"} if allow_no_text else LLM
            requirements = [llm, ACOUSTIC, ASR]
        plugin, registry = make_registry(requirements, allow_no_text, version2)
        task = write(
            db,
            bank,
            replace(
                CONTENT,
                task_type="qa",
                guide_text=None,
                reference_answer="private reference",
                scoring_instruction="frozen scoring",
            ),
        )
        paper = draft(db, bank)
        with Session(db) as s, s.begin():
            add_entry(s, paper_id=paper, expected_version=0, task_id=task.task_id, position=1)
            select_plugin(
                s,
                paper_id=paper,
                expected_version=1,
                plugin_id="test-only",
                version="1",
                registry=registry,
            )
            publish(
                s,
                paper_id=paper,
                expected_version=2,
                registry=registry,
                audio_config=TEST_AUDIO_CONFIG,
            )
        fixed, store = AudioStore(tmp_path / "fixed"), RecordingStore(tmp_path / "recordings")
        prepare_audio(db, paper, fixed, Synth())
        app.dependency_overrides[get_audio_store] = lambda: fixed
        app.dependency_overrides[get_recording_store] = lambda: store
        app.dependency_overrides[get_scoring_registry] = lambda: registry
        response = client.post(
            "/teacher/assessment/sessions", headers=AUTH, json=create_body(paper)
        )
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        headers = access(issue(client, sid))
        root = f"/assessment/sessions/{sid}"
        tasks = client.get(root, headers=headers).json()["tasks"]
        f = {
            "sid": sid,
            "root": root,
            "headers": headers,
            "tasks": tasks,
            "store": store,
            "registry": registry,
            "plugin": plugin,
            "no_text": no_text,
        }
        aid = deliver(client, f)
        rid = record(client, f, aid).json()["recording_id"]
        assert upload(client, f, aid, rid).status_code == 200
        submission = submit(client, f, aid, rid)
        assert submission.status_code == 200
        f.update(
            {
                "attempt_id": aid,
                "recording_id": rid,
                "submission_id": submission.json()["submission_id"],
            }
        )
        if close:
            assert (
                client.post(
                    root + "/close", headers=headers, json={"close_request_id": "close"}
                ).status_code
                == 200
            )
        with Session(db) as s, s.begin():
            assert schedule(s) == len(requirements)
        with Session(db) as s:
            f["jobs"] = {
                j.requirement["source"]: j.job_id for j in s.scalars(select(EvaluationJob))
            }
        return f

    return create


class Asr:
    def __init__(self, flow):
        self.flow, self.calls, self.fail = flow, 0, False

    def resolve_inputs(self, db, submission, revision, requirement):
        audio = recording_input(db, submission)
        return InputSnapshot(
            refs={k: audio[k] for k in ("recording_id", "sha256", "media_type")},
            payload={
                "storage_ref": audio["storage_ref"],
                "sha256": audio["sha256"],
                "media_type": audio["media_type"],
            },
        )

    def evaluate(self, inputs, requirement):
        self.calls += 1
        if self.fail:
            self.fail = False
            raise KnownFailure("TEST_ASR_FAILURE")
        assert inputs.payload["media_type"] == "audio/wav"
        try:
            data = self.flow["store"].read(inputs.payload["storage_ref"], inputs.payload["sha256"])
        except ValueError as exc:
            raise KnownFailure("RECORDING_UNAVAILABLE") from exc
        assert data == wav()
        return AdapterResult(
            payload={"status": "NO_TEXT", "text": None}
            if self.flow["no_text"]
            else {"status": "TEXT", "text": "transcribed words"},
            provider="test-provider",
            model="test-asr-model",
            engine_version="test-1",
        )

    def validate_result(self, result, requirement):
        return True


class Llm:
    def __init__(self):
        self.calls, self.fail, self.received = 0, False, []

    def resolve_inputs(self, db, submission, revision, requirement):
        return InputSnapshot(
            refs={"context_version": "test-1"},
            payload={"scoring_instruction": revision.scoring_instruction},
        )

    def evaluate(self, inputs, requirement):
        self.calls += 1
        self.received.append(inputs)
        assert inputs.payload["scoring_instruction"] == "frozen scoring"
        if self.fail:
            self.fail = False
            raise KnownFailure("TEST_LLM_FAILURE")
        return AdapterResult(payload={"raw_metric": 0.7})

    def validate_result(self, result, requirement):
        return True


class Acoustic:
    def __init__(self):
        self.calls = 0

    def resolve_inputs(self, db, submission, revision, requirement):
        return InputSnapshot(refs={"recording_id": submission.inputs["recording_id"]}, payload={})

    def evaluate(self, inputs, requirement):
        self.calls += 1
        return AdapterResult(payload={"raw_metric": 0.5})

    def validate_result(self, result, requirement):
        return True


def claim(db, f, source, adapters):
    return claim_job(
        db, f["jobs"][source], registry=f["registry"], adapters=adapters, lease=timedelta(minutes=1)
    )


def adapters(f):
    asr, llm, acoustic = Asr(f), Llm(), Acoustic()
    return (
        asr,
        llm,
        acoustic,
        {
            ("test-asr", "1"): asr,
            ("test-llm", "1"): llm,
            ("test-llm", "2"): llm,
            ("test-acoustic", "1"): acoustic,
        },
    )


def test_dependency_success_and_independent_source(db, client, dependency_flow):
    f = dependency_flow()
    asr, llm, acoustic, sources = adapters(f)
    assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
    assert execute_claim(db, claim(db, f, "test-acoustic", sources)) and acoustic.calls == 1
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    work = claim(db, f, "test-llm", sources)
    assert work.inputs.payload["asr"] == {"status": "TEXT", "text": "transcribed words"}
    with Session(db) as s:
        raw_asr = s.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == f["jobs"]["test-asr"], RawEvaluation.status == "READY"
            )
        )
        assert work.inputs.refs["asr"]["evaluation_id"] == str(raw_asr.evaluation_id)
        assert work.inputs.refs["asr"]["input_fingerprint"] == raw_asr.input_fingerprint
        assert work.inputs.refs["asr"]["recording_id"] == f["recording_id"]
        assert work.inputs.refs["asr"]["submission_id"] == f["submission_id"]
        assert work.inputs.refs["asr"]["provider"] == "test-provider"
        assert work.inputs.refs["asr"]["model"] == "test-asr-model"
        assert work.inputs.refs["asr"]["engine_version"] == "test-1"
    assert execute_claim(db, work)
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    assert client.get(f["root"], headers=f["headers"]).json()["status"] == "COMPLETED"
    with Session(db) as s:
        raws = list(s.scalars(select(RawEvaluation)))
        assert len(raws) == 3 and len({r.evaluation_id for r in raws}) == 3
        assert s.scalar(select(FinalScoreResult)) is not None
    out = run_once(
        db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
    )
    assert out["claimed"] == 0 and (asr.calls, llm.calls, acoustic.calls) == (1, 1, 1)
    assert f["plugin"].calls == ["task", "paper"]


@pytest.mark.parametrize("allowed", [False, True])
def test_no_credible_text_respects_downstream_contract(db, dependency_flow, allowed):
    f = dependency_flow(no_text=True, allow_no_text=allowed)
    _asr, llm, _acoustic, sources = adapters(f)
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert execute_claim(db, claim(db, f, "test-acoustic", sources))
    work = claim(db, f, "test-llm", sources)
    if allowed:
        assert work.inputs.payload["asr"] == {"status": "NO_TEXT", "text": None}
        assert execute_claim(db, work)
        assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    else:
        assert work is None and llm.calls == 0
        assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
        with Session(db) as s:
            assert s.scalar(select(FinalScoreResult)) is None


def test_asr_and_llm_failures_retry_independently(db, dependency_flow):
    f = dependency_flow()
    asr, llm, acoustic, sources = adapters(f)
    asr.fail = True
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
    with Session(db) as s, s.begin():
        assert s.get(EvaluationJob, f["jobs"]["test-asr"]).status == "FAILED"
        authorize_retry(s, f["jobs"]["test-asr"])
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert execute_claim(db, claim(db, f, "test-acoustic", sources))
    llm.fail = True
    assert execute_claim(db, claim(db, f, "test-llm", sources))
    with Session(db) as s, s.begin():
        assert s.get(EvaluationJob, f["jobs"]["test-llm"]).status == "FAILED"
        authorize_retry(s, f["jobs"]["test-llm"])
    assert execute_claim(db, claim(db, f, "test-llm", sources))
    assert (asr.calls, llm.calls, acoustic.calls) == (2, 2, 1)
    with Session(db) as s:
        assert [
            r.status
            for r in s.scalars(
                select(RawEvaluation)
                .where(RawEvaluation.job_id == f["jobs"]["test-asr"])
                .order_by(RawEvaluation.created_at)
            )
        ] == ["FAILED", "READY"]
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1


def test_llm_expired_claim_preserves_frozen_upstream_and_rejects_changed_input(
    db, client, dependency_flow
):
    from sqlalchemy import text

    from app.evaluation.worker import recover_expired

    f = dependency_flow()
    _, llm, _, sources = adapters(f)
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    first = claim(db, f, "test-llm", sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-llm"])
        refs, fingerprint = job.input_refs, job.input_fingerprint
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE evaluation_jobs SET lease_until = clock_timestamp() - interval '1 second' WHERE job_id = :id"
            ),
            {"id": f["jobs"]["test-llm"]},
        )
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    resolve = llm.resolve_inputs

    def changed(db_session, submission, revision, requirement):
        snapshot = resolve(db_session, submission, revision, requirement)
        snapshot.payload["scoring_instruction"] = "changed context"
        return snapshot

    llm.resolve_inputs = changed
    with pytest.raises(ValueError, match="inputs changed after first claim"):
        claim(db, f, "test-llm", sources)
    with Session(db) as s:
        failed = s.get(EvaluationJob, f["jobs"]["test-llm"])
        assert failed.status == "WAITING" and failed.error_code == "INPUT_IDENTITY_CONFLICT"
        assert failed.input_refs == refs and failed.input_fingerprint == fingerprint
    diagnostic = client.get(f"/teacher/sessions/{f['sid']}/scoring", headers=AUTH).json()
    source = next(
        item for item in diagnostic["tasks"][0]["evaluations"] if item["source"] == "test-llm"
    )
    assert source["error_code"] == "INPUT_IDENTITY_CONFLICT"
    llm.resolve_inputs = resolve
    second = claim(db, f, "test-llm", sources)
    assert second and second.token != first.token
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-llm"])
        assert job.input_refs == refs and job.input_fingerprint == fingerprint
    second.inputs.payload["asr"]["text"] = "changed after claim"
    with pytest.raises(ValueError, match="frozen input identity"):
        execute_claim(db, second)
    assert llm.calls == 0
    second.inputs.payload["asr"]["text"] = "transcribed words"
    assert execute_claim(db, second)
    assert llm.calls == 1


@pytest.mark.parametrize("batch_size", [1, 2])
def test_input_conflict_isolated_and_scan_fair_after_restart(db, dependency_flow, batch_size):
    from app.evaluation.worker import recover_expired

    f = dependency_flow()
    asr, llm, acoustic, sources = adapters(f)
    first = claim(db, f, "test-asr", sources)
    assert first is not None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        refs, fingerprint = job.input_refs, job.input_fingerprint
    with db.begin() as connection:
        connection.execute(
            text(
                "UPDATE evaluation_jobs SET lease_until = clock_timestamp() - interval '1 second' "
                "WHERE job_id = :id"
            ),
            {"id": f["jobs"]["test-asr"]},
        )
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    with db.begin() as connection:
        for source, seconds in (("test-asr", 30), ("test-acoustic", 20), ("test-llm", 10)):
            connection.execute(
                text(
                    "UPDATE evaluation_jobs SET last_checked_at = "
                    "clock_timestamp() - (:seconds * interval '1 second') WHERE job_id = :id"
                ),
                {"id": f["jobs"][source], "seconds": seconds},
            )
    with Session(db) as s:
        old_checked = s.get(EvaluationJob, f["jobs"]["test-asr"]).last_checked_at

    resolve = asr.resolve_inputs

    def mismatch(resolver):
        def changed(db_session, submission, revision, requirement):
            snapshot = resolver(db_session, submission, revision, requirement)
            snapshot.payload["media_type"] = "changed"
            return snapshot

        return changed

    asr.resolve_inputs = mismatch(resolve)
    result = run_once(
        db,
        registry=f["registry"],
        adapters=sources,
        lease=timedelta(minutes=1),
        batch_size=batch_size,
    )
    assert result["claimed"] == batch_size - 1
    assert result["finished"] == batch_size - 1
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        assert job.status == "WAITING" and job.error_code == "INPUT_IDENTITY_CONFLICT"
        assert job.last_checked_at > old_checked
        assert job.input_refs == refs and job.input_fingerprint == fingerprint
        assert s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id)) is None
        existing_raws = {
            raw.evaluation_id: (raw.status, raw.input_refs, raw.input_fingerprint, raw.payload)
            for raw in s.scalars(select(RawEvaluation))
        }
    assert asr.calls == 0 and acoustic.calls == batch_size - 1
    with pytest.raises(InputIdentityConflict):
        claim(db, f, "test-asr", sources)
    with Session(db) as s:
        assert {
            raw.evaluation_id: (raw.status, raw.input_refs, raw.input_fingerprint, raw.payload)
            for raw in s.scalars(select(RawEvaluation))
        } == existing_raws

    if batch_size == 1:
        # Dispose connections and rebuild worker dependencies to model a restart.
        db.dispose()
        _, registry = make_registry([LLM, ACOUSTIC, ASR])
        asr, llm, acoustic, sources = adapters(f)
        resolve = asr.resolve_inputs
        asr.resolve_inputs = mismatch(resolve)
        result = run_once(
            db, registry=registry, adapters=sources, lease=timedelta(minutes=1), batch_size=1
        )
        assert result["claimed"] == result["finished"] == 1
    assert acoustic.calls == 1 and asr.calls == llm.calls == 0
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        assert job.input_refs == refs and job.input_fingerprint == fingerprint
        assert s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id)) is None

    asr.resolve_inputs = resolve
    for _ in range(2):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    assert (asr.calls, llm.calls, acoustic.calls) == (1, 1, 1)
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    with Session(db) as s:
        raw_ids = set(s.scalars(select(RawEvaluation.evaluation_id)))
        result_ids = set(s.scalars(select(FinalScoreResult.result_id)))
        assert len(raw_ids) == 3 and len(result_ids) == 1
    assert (
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )["claimed"]
        == 0
    )
    run_settlement_once(db, registry=f["registry"])
    with Session(db) as s:
        assert set(s.scalars(select(RawEvaluation.evaluation_id))) == raw_ids
        assert set(s.scalars(select(FinalScoreResult.result_id))) == result_ids
    assert (asr.calls, llm.calls, acoustic.calls) == (1, 1, 1)


def test_concurrent_asr_claim_and_waiting_llm(db, dependency_flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    f = dependency_flow()
    asr, llm, _, sources = adapters(f)
    barrier = Barrier(2)

    def acquire():
        barrier.wait(timeout=10)
        return claim(db, f, "test-asr", sources)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(acquire), pool.submit(acquire)]
        claims = [result.result(timeout=10) for result in results]
    assert sum(c is not None for c in claims) == 1
    entered, release = Event(), Event()
    original = asr.evaluate

    def held(inputs, requirement):
        entered.set()
        assert release.wait(10)
        return original(inputs, requirement)

    asr.evaluate = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, next(c for c in claims if c is not None))
        try:
            assert entered.wait(10)
            assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
        finally:
            release.set()
        assert future.result(timeout=10)
    work = claim(db, f, "test-llm", sources)
    assert work and execute_claim(db, work)
    with Session(db) as s:
        rows = list(
            s.scalars(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == f["jobs"]["test-asr"], RawEvaluation.status == "READY"
                )
            )
        )
        assert len(rows) == 1
    assert asr.calls == llm.calls == 1


def test_recognition_rejects_wrong_recording_identity(db, dependency_flow):
    f = dependency_flow()
    asr, _, _, sources = adapters(f)
    original = asr.resolve_inputs

    def wrong(db_session, submission, revision, requirement):
        inputs = original(db_session, submission, revision, requirement)
        inputs.refs["recording_id"] = "00000000-0000-0000-0000-000000000000"
        return inputs

    asr.resolve_inputs = wrong
    with pytest.raises(ValueError, match="recording identity mismatch"):
        claim(db, f, "test-asr", sources)
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"]["test-asr"]).status == "WAITING"
        assert s.scalar(select(RawEvaluation)) is None


@pytest.mark.parametrize("stage", ["asr", "llm"])
def test_redo_separates_old_and_new_evidence(db, client, dependency_flow, stage):
    f = dependency_flow(close=False)
    asr, llm, _, sources = adapters(f)
    if stage == "asr":
        old_claim = claim(db, f, "test-asr", sources)
    else:
        assert execute_claim(db, claim(db, f, "test-asr", sources))
        old_claim = claim(db, f, "test-llm", sources)
    task_path = f"{f['root']}/tasks/{f['tasks'][0]['session_task_id']}"
    redo = client.post(task_path + "/redo", headers=f["headers"], json={"redo_request_id": "redo"})
    assert redo.status_code == 200
    assert not execute_claim(db, old_claim)
    with Session(db) as s:
        assert s.get(EvaluationJob, old_claim.job_id).status == "SKIPPED"
    new_attempt = deliver(client, f)
    assert new_attempt != f["attempt_id"]
    rid = record(client, f, new_attempt, req="new").json()["recording_id"]
    assert upload(client, f, new_attempt, rid).status_code == 200
    new_submission = submit(client, f, new_attempt, rid, request="new").json()["submission_id"]
    assert new_submission != f["submission_id"]
    with Session(db) as s, s.begin():
        assert schedule(s) == 3
    with Session(db) as s:
        new_jobs = {
            j.requirement["source"]: j.job_id
            for j in s.scalars(
                select(EvaluationJob).where(EvaluationJob.submission_id == UUID(new_submission))
            )
        }
    fresh = {**f, "jobs": new_jobs, "recording_id": rid, "submission_id": new_submission}
    assert claim(db, fresh, "test-llm", sources) is None
    assert execute_claim(db, claim(db, fresh, "test-asr", sources))
    new_llm = claim(db, fresh, "test-llm", sources)
    assert new_llm.inputs.refs["asr"]["recording_id"] == rid
    assert new_llm.inputs.refs["asr"]["job_id"] == str(new_jobs["test-asr"])
    assert execute_claim(db, new_llm)
    assert execute_claim(db, claim(db, fresh, "test-acoustic", sources))
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    assert not execute_claim(db, old_claim)
    with Session(db) as s:
        final = s.scalar(select(FinalScoreResult))
        assert final is not None
        old_llm_ready = list(
            s.scalars(
                select(RawEvaluation).where(
                    RawEvaluation.submission_id == UUID(f["submission_id"]),
                    RawEvaluation.status == "READY",
                    RawEvaluation.job_id == f["jobs"]["test-llm"],
                )
            )
        )
        assert old_llm_ready == []
    assert llm.calls == 1 and asr.calls == (1 if stage == "asr" else 2)


@pytest.mark.parametrize(
    "ref",
    [
        {"requirement_index": 3, "source": "test-asr", "version": "1"},
        {"requirement_index": 1, "source": "test-asr", "version": "1"},
        {"requirement_index": 0, "source": "test-llm", "version": "1"},
        {"requirement_index": 2, "source": "test-asr", "version": "2"},
        {"requirement_index": True, "source": "test-asr", "version": "1"},
        {"requirement_index": 2, "source": "test-asr"},
    ],
)
def test_publication_rejects_invalid_upstream_reference(db, bank, ref):
    from app.papers.publication import PublicationBlocked

    bad = {**LLM, "depends_on": ref}
    _, registry = make_registry([bad, ACOUSTIC, ASR])
    task = write(db, bank, replace(CONTENT, task_type="qa", guide_text=None))
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        add_entry(s, paper_id=paper, expected_version=0, task_id=task.task_id, position=1)
        select_plugin(
            s,
            paper_id=paper,
            expected_version=1,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        preflight = preflight_publication(s, paper, registry)
        assert not preflight.valid
        assert "Evaluation dependency plan is invalid" in preflight.entries[0].blockers
        with pytest.raises(PublicationBlocked):
            publish(
                s,
                paper_id=paper,
                expected_version=2,
                registry=registry,
                audio_config=TEST_AUDIO_CONFIG,
            )


def test_explicit_index_disambiguates_versions_and_duplicate_sources():
    from copy import deepcopy

    plan = [deepcopy(LLM), {**ASR, "version": "2"}, deepcopy(ASR), deepcopy(ASR)]
    _, base_registry = make_registry(plan)
    contract2 = EvaluationContract(
        "test-asr", "2", frozenset({"qa"}), lambda p: p == {}, False, recognition_result=lambda p: p
    )
    contracts = list(base_registry._contracts.values()) + [contract2]
    registry = Registry([Plugin(plan)], contracts)
    assert registry.valid_plan("qa", plan)
    wrong = deepcopy(plan)
    wrong[0]["depends_on"]["requirement_index"] = 1
    assert not registry.valid_plan("qa", wrong)
    duplicate = deepcopy(plan)
    duplicate[0]["depends_on"]["requirement_index"] = 3
    assert registry.valid_plan("qa", duplicate)


def test_unknown_asr_requires_explicit_retry(db, dependency_flow):
    f = dependency_flow()
    asr, llm, _, sources = adapters(f)
    original = asr.evaluate

    def unknown(inputs, requirement):
        asr.calls += 1
        raise RuntimeError("unknown external outcome")

    asr.evaluate = unknown
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
    with Session(db) as s, s.begin():
        assert s.get(EvaluationJob, f["jobs"]["test-asr"]).status == "UNKNOWN"
        with pytest.raises(ValueError, match="explicit acknowledgment"):
            authorize_retry(s, f["jobs"]["test-asr"])
        authorize_retry(s, f["jobs"]["test-asr"], accept_unknown=True)
    asr.evaluate = original
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert execute_claim(db, claim(db, f, "test-llm", sources))
    assert asr.calls == 2 and llm.calls == 1


def test_late_old_llm_evidence_does_not_change_new_formal_result(db, client, dependency_flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    f = dependency_flow(close=False)
    _, llm, _, sources = adapters(f)
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    old_claim = claim(db, f, "test-llm", sources)
    entered, release = Event(), Event()
    evaluate = llm.evaluate

    def held(inputs, requirement):
        if inputs.refs["asr"]["recording_id"] == f["recording_id"]:
            entered.set()
            assert release.wait(15)
        return evaluate(inputs, requirement)

    llm.evaluate = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, old_claim)
        try:
            assert entered.wait(10)
            task_path = f"{f['root']}/tasks/{f['tasks'][0]['session_task_id']}"
            assert (
                client.post(
                    task_path + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
                ).status_code
                == 200
            )
            aid = deliver(client, f)
            rid = record(client, f, aid, req="new").json()["recording_id"]
            assert upload(client, f, aid, rid).status_code == 200
            new_submission = submit(client, f, aid, rid, request="new").json()["submission_id"]
            with Session(db) as s, s.begin():
                assert schedule(s) == 3
            with Session(db) as s:
                jobs = {
                    j.requirement["source"]: j.job_id
                    for j in s.scalars(
                        select(EvaluationJob).where(
                            EvaluationJob.submission_id == UUID(new_submission)
                        )
                    )
                }
            fresh = {**f, "jobs": jobs}
            assert execute_claim(db, claim(db, fresh, "test-asr", sources))
            assert execute_claim(db, claim(db, fresh, "test-llm", sources))
            assert execute_claim(db, claim(db, fresh, "test-acoustic", sources))
            assert (
                client.post(
                    f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
                ).status_code
                == 200
            )
            assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
            with Session(db) as s:
                result_id = s.scalar(select(FinalScoreResult.result_id))
        finally:
            release.set()
        assert future.result(timeout=10)
    with Session(db) as s:
        assert s.scalar(select(FinalScoreResult.result_id)) == result_id
        old = s.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == old_claim.job_id, RawEvaluation.status == "READY"
            )
        )
        assert old is not None and str(old.submission_id) == f["submission_id"]
    assert f["plugin"].calls == ["task", "paper"]


@pytest.mark.parametrize("requirements", [[], [ACOUSTIC]])
def test_only_declared_sources_are_dispatched(db, dependency_flow, requirements):
    f = dependency_flow(requirements=requirements)
    asr, llm, acoustic, sources = adapters(f)
    result = run_once(
        db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
    )
    assert result["finished"] == len(requirements)
    assert (asr.calls, llm.calls, acoustic.calls) == (0, 0, len(requirements))
    with Session(db) as s:
        jobs = list(s.scalars(select(EvaluationJob)))
        assert [job.requirement["source"] for job in jobs] == [x["source"] for x in requirements]
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1


def test_missing_recording_is_failure_not_recognition(db, dependency_flow):
    from app.media.recording import Recording

    f = dependency_flow()
    _asr, llm, _, sources = adapters(f)
    with Session(db) as s:
        recording = s.get(Recording, UUID(f["recording_id"]))
        f["store"].path(recording.storage_ref, recording.sha256).unlink()
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert job.status == "FAILED" and raw.status == "FAILED" and raw.payload is None
        assert s.scalar(select(FinalScoreResult)) is None


def test_redo_lock_serializes_before_asr_claim(db, dependency_flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from app.sessions.assessment_session import redo_attempt

    f = dependency_flow(close=False)
    _, _, _, sources = adapters(f)
    entered, release, competing = Event(), Event(), Event()

    def redo():
        with Session(db) as s, s.begin():
            redo_attempt(
                s,
                session_id=UUID(f["sid"]),
                session_task_id=UUID(f["tasks"][0]["session_task_id"]),
                redo_request_id="redo",
            )
            entered.set()
            assert release.wait(10)

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "assessment_sessions" in statement and "FOR UPDATE" in statement:
            competing.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(redo)
        assert entered.wait(10)
        event.listen(db, "before_cursor_execute", observe)
        try:
            second = pool.submit(claim, db, f, "test-asr", sources)
            assert competing.wait(10)
        finally:
            event.remove(db, "before_cursor_execute", observe)
            release.set()
        first.result(timeout=10)
        assert second.result(timeout=10) is None
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"]["test-asr"]).status == "SKIPPED"
        assert s.scalar(select(RawEvaluation)) is None


def test_worker_restart_resolves_persisted_upstream(db, dependency_flow):
    f = dependency_flow()
    asr, _, _, sources = adapters(f)
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    db.dispose()
    _, registry = make_registry([LLM, ACOUSTIC, ASR])
    fresh = {**f, "registry": registry}
    _, llm, _, fresh_sources = adapters(fresh)
    work = claim(db, fresh, "test-llm", fresh_sources)
    assert work.inputs.payload["asr"]["text"] == "transcribed words"
    assert execute_claim(db, work) and asr.calls == llm.calls == 1


def test_multiple_asr_versions_resolve_only_referenced_evidence(db, dependency_flow):
    llm_requirement = {
        **LLM,
        "depends_on": {"requirement_index": 3, "source": "test-asr", "version": "1"},
    }
    requirements = [{**ASR, "version": "2"}, llm_requirement, ACOUSTIC, ASR]
    f = dependency_flow(requirements=requirements, version2=True)
    asr1, llm, acoustic, _ = adapters(f)
    asr2 = Asr(f)
    original = asr2.evaluate

    def other(inputs, requirement):
        original(inputs, requirement)
        return AdapterResult(payload={"status": "TEXT", "text": "different source version"})

    asr2.evaluate = other
    sources = {
        ("test-asr", "1"): asr1,
        ("test-asr", "2"): asr2,
        ("test-llm", "1"): llm,
        ("test-llm", "2"): llm,
        ("test-acoustic", "1"): acoustic,
    }
    with Session(db) as s:
        rows = list(s.scalars(select(EvaluationJob)))
        by_index = {job.requirement_index: job.job_id for job in rows}

    def claim_index(index):
        return claim_job(
            db,
            by_index[index],
            registry=f["registry"],
            adapters=sources,
            lease=timedelta(minutes=1),
        )

    assert execute_claim(db, claim_index(0))
    assert claim_index(1) is None
    assert execute_claim(db, claim_index(3))
    downstream = claim_index(1)
    assert downstream.inputs.refs["asr"]["job_id"] == str(by_index[3])
    assert downstream.inputs.refs["asr"]["version"] == "1"
    assert downstream.inputs.payload["asr"]["text"] == "transcribed words"
    assert execute_claim(db, downstream)


def test_invalid_recognition_payload_never_becomes_ready(db, dependency_flow):
    f = dependency_flow()
    asr, llm, _, sources = adapters(f)
    asr.evaluate = lambda inputs, requirement: AdapterResult(
        payload={"status": "TEXT", "text": None}
    )
    assert execute_claim(db, claim(db, f, "test-asr", sources))
    assert claim(db, f, "test-llm", sources) is None and llm.calls == 0
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert job.status == "INVALID" and raw.status == "INVALID" and raw.payload is None
