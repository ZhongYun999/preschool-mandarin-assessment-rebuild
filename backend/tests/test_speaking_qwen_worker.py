"""Formal Speaking ASR uses PostgreSQL facts and a non-network provider."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_speaking_evaluation_input import OfflineAdapter, make_case, valid_consumption

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.speaking_asr_models import SpeakingFormalASRAttempt, SpeakingFormalASROperation
from app.evaluation.speaking_qwen_contract import (
    SOURCE,
    VERSION,
    valid_full_consumption,
    valid_parameters,
    validate_result,
)
from app.evaluation.speaking_qwen_worker import (
    aggregate,
    authorize_retry,
    claim,
    execute,
    prepare,
    recover_expired,
)
from app.evaluation.worker import KnownFailure
from app.evaluation.worker import claim_job as claim_evaluation_job
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.registry import PRODUCTION_REGISTRY, EvaluationContract

pytest_plugins = ("test_speaking_process_asr",)


def setup_case(db, client, flow, monkeypatch, tmp_path, count):
    tools = AudioTools()
    spec = AudioSpec("wav", "pcm_s16le", 16000, 1, "1", tools.version(), tools.version(probe=True))
    params = {
        "provider": "dashscope",
        "model": "qwen3-asr-flash",
        "region": "cn-beijing",
        "workspace": "test-workspace",
        "enable_itn": False,
        "language": "zh",
        "audio_spec": asdict(spec),
        "processing_version": "1",
        "confirmed_no_text": False,
    }
    requirement = {"source": SOURCE, "version": VERSION, "parameters": params}
    assert PRODUCTION_REGISTRY.supports("speaking", requirement)
    assert not PRODUCTION_REGISTRY.supports("qa", requirement)
    contract = EvaluationContract(
        SOURCE,
        VERSION,
        frozenset({"speaking"}),
        valid_parameters,
        speaking_consumption_validator=valid_full_consumption,
        speaking_recognition_result=validate_result,
    )
    case = make_case(db, client, flow, monkeypatch, count, [requirement], [contract])
    return case, tools, RecordingStore(tmp_path / "derived")


class Provider:
    def __init__(self, fail=None):
        self.calls = 0
        self.fail = fail

    def recognize(self, data, params):
        self.calls += 1
        assert data[:4] == b"RIFF"
        if self.calls == self.fail:
            raise KnownFailure("ASR_QUOTA")
        return {
            "status": "TEXT",
            "text": f"fixture-{self.calls}",
            "request_id": f"request-{self.calls}",
        }


def test_three_segments_failure_and_explicit_recovery(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 3)
    _, _, _, _, submission, _, store, ids = case
    job = ids[0]
    assert prepare(db, job, store, derived, tools)
    provider = Provider(fail=3)
    lease = timedelta(minutes=1)
    for _ in range(3):
        operation, token = claim(db, job, lease=lease)
        assert execute(db, operation, token, store, derived, provider)
    with Session(db) as session, session.begin():
        rows = list(
            session.scalars(
                select(SpeakingFormalASROperation)
                .where(SpeakingFormalASROperation.job_id == job)
                .order_by(SpeakingFormalASROperation.position)
            )
        )
        assert [row.status for row in rows] == ["READY", "READY", "FAILED"]
        assert session.get(EvaluationJob, job).status == "FAILED"
        assert session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job)) is None
        authorize_retry(session, rows[2].operation_id)
    assert not aggregate(db, job)
    operation, token = claim(db, job, lease=lease)
    assert execute(db, operation, token, store, derived, provider)
    assert provider.calls == 4
    assert aggregate(db, job) and aggregate(db, job)
    with Session(db) as session:
        raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == job, RawEvaluation.status == "READY"
            )
        )
        assert raw.submission_id == submission
        assert [s["text"] for s in raw.payload["segments"]] == [
            "fixture-1",
            "fixture-2",
            "fixture-4",
        ]


def test_expired_before_and_after_dispatch(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    op, token = claim(db, ids[0], lease=timedelta(microseconds=1))
    with Session(db) as session, session.begin():
        assert recover_expired(session) == 1
        assert session.get(SpeakingFormalASROperation, op).status == "WAITING"
    assert not execute(db, op, token, store, derived, Provider())


def test_unknown_needs_explicit_authorization_and_old_token_is_fenced(
    db, client, flow, monkeypatch, tmp_path
):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    op, token = claim(db, ids[0], lease=timedelta(minutes=1))
    with Session(db) as session, session.begin():
        attempt = session.get(SpeakingFormalASRAttempt, token)
        attempt.dispatch_started_at = session.scalar(select(func.clock_timestamp()))
        session.get(SpeakingFormalASROperation, op).lease_until = (
            attempt.dispatch_started_at - timedelta(seconds=1)
        )
    with Session(db) as session, session.begin():
        assert recover_expired(session) == 1
        assert session.get(SpeakingFormalASROperation, op).status == "UNKNOWN"
        assert session.get(EvaluationJob, ids[0]).status == "UNKNOWN"
        with pytest.raises(ValueError, match="authorization"):
            authorize_retry(session, op)
    assert claim(db, ids[0], lease=timedelta(minutes=1)) is None
    with Session(db) as session, session.begin():
        authorize_retry(session, op, accept_unknown=True)
    new_op, new_token = claim(db, ids[0], lease=timedelta(minutes=1))
    assert new_op == op and new_token != token
    provider = Provider()
    assert not execute(db, op, token, store, derived, provider)
    assert provider.calls == 0
    assert execute(db, op, new_token, store, derived, provider)
    assert provider.calls == 1
    assert aggregate(db, ids[0])


@pytest.mark.parametrize("count", [1, 2, 3])
def test_every_segment_succeeds_and_input_is_frozen(db, client, flow, monkeypatch, tmp_path, count):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, count)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    with Session(db) as session:
        refs = session.get(EvaluationJob, ids[0]).input_refs
        assert len(refs["speaking_source"]["segments"]) == count
        assert len(refs["dependencies"]["speaking_consumption"]["audio_inputs"]) == count
    provider = Provider()
    for _ in range(count):
        operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
        assert execute(db, operation, token, store, derived, provider)
    assert aggregate(db, ids[0])
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert len(raw.payload["segments"]) == count
        assert session.get(EvaluationJob, ids[0]).input_refs == refs


def test_concurrent_workers_cannot_claim_same_segment(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(db, ids[0], lease=timedelta(minutes=1)), range(2)))
    assert sum(result is not None for result in results) == 1
    operation, token = next(result for result in results if result is not None)
    provider = Provider()
    assert execute(db, operation, token, store, derived, provider)
    assert provider.calls == 1


def test_missing_audio_fails_before_network(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
    with Session(db) as session:
        audio = session.get(SpeakingFormalASROperation, operation).audio_input
        source = session.get(EvaluationJob, ids[0]).input_refs["speaking_source"]["segments"][0]
    used = store if audio["storage_ref"] == source["storage_ref"] else derived
    used.path(audio["storage_ref"], audio["sha256"]).unlink()
    provider = Provider()
    assert execute(db, operation, token, store, derived, provider)
    assert provider.calls == 0
    with Session(db) as session:
        assert session.get(SpeakingFormalASROperation, operation).error_code == "AUDIO_UNAVAILABLE"


def test_converted_audio_is_frozen_and_read_from_derived_store(
    db, client, flow, monkeypatch, tmp_path
):
    import test_speaking_process_asr
    from test_audio_preparation import make_wav

    monkeypatch.setattr(test_speaking_process_asr, "wav", lambda: make_wav(rate=8000))
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        consumption = job.input_refs["dependencies"]["speaking_consumption"]
        original = job.input_refs["speaking_source"]["segments"][0]
        audio = consumption["audio_inputs"][0]
        assert len(consumption["derived_recordings"]) == 1
        assert audio["sha256"] != original["sha256"]
        assert derived.read(audio["storage_ref"], audio["sha256"]).startswith(b"RIFF")
        fingerprint = job.input_fingerprint
    assert prepare(db, ids[0], store, derived, tools)
    operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
    assert execute(db, operation, token, store, derived, Provider())
    assert aggregate(db, ids[0])
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert raw.input_fingerprint == fingerprint
        assert raw.payload["segments"][0]["input_sha256"] == audio["sha256"]


def test_generic_worker_never_dispatches_segment_source(db, client, flow, monkeypatch, tmp_path):
    case, _, _ = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, registry, store, ids = case
    adapter = OfflineAdapter()
    assert (
        claim_evaluation_job(
            db,
            ids[0],
            registry=registry,
            adapters={(SOURCE, VERSION): adapter},
            lease=timedelta(minutes=1),
            recording_store=store,
        )
        is None
    )
    assert adapter.calls == 0
    with Session(db) as session:
        assert (
            session.get(EvaluationJob, ids[0]).error_code
            == "EVALUATION_REQUIRES_SEGMENT_COORDINATOR"
        )


def test_formal_asr_handoff_preserves_evidence_identity(db, client, flow, monkeypatch, tmp_path):
    tools = AudioTools()
    spec = AudioSpec("wav", "pcm_s16le", 16000, 1, "1", tools.version(), tools.version(probe=True))
    params = {
        "provider": "dashscope",
        "model": "qwen3-asr-flash",
        "region": "cn-beijing",
        "workspace": "test-workspace",
        "enable_itn": False,
        "language": "zh",
        "audio_spec": asdict(spec),
        "processing_version": "1",
        "confirmed_no_text": False,
    }
    asr = {"source": SOURCE, "version": VERSION, "parameters": params}
    text = {
        "source": "offline-text",
        "version": "1",
        "parameters": {},
        "depends_on": {"requirement_index": 0, "source": SOURCE, "version": VERSION},
    }
    contracts = [
        EvaluationContract(
            SOURCE,
            VERSION,
            frozenset({"speaking"}),
            valid_parameters,
            speaking_consumption_validator=valid_full_consumption,
            speaking_recognition_result=validate_result,
        ),
        EvaluationContract(
            "offline-text",
            "1",
            frozenset({"speaking"}),
            lambda p: p == {},
            accepts_no_text=False,
            speaking_consumption_validator=valid_consumption,
        ),
    ]
    case = make_case(db, client, flow, monkeypatch, 2, [asr, text], contracts)
    _, _, _, _, _, registry, store, ids = case
    derived = RecordingStore(tmp_path / "derived")
    assert prepare(db, ids[0], store, derived, tools)
    provider = Provider()
    for _ in range(2):
        work = claim(db, ids[0], lease=timedelta(minutes=1))
        assert execute(db, *work, store, derived, provider)
    assert aggregate(db, ids[0])
    downstream = claim_evaluation_job(
        db,
        ids[1],
        registry=registry,
        adapters={("offline-text", "1"): OfflineAdapter()},
        lease=timedelta(minutes=1),
        recording_store=store,
        derived_recording_store=derived,
    )
    assert [segment["text"] for segment in downstream.inputs.payload["asr"]["segments"]] == [
        "fixture-1",
        "fixture-2",
    ]
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert downstream.inputs.refs["asr"]["evaluation_id"] == str(raw.evaluation_id)
        assert downstream.inputs.refs["asr"]["input_fingerprint"] == raw.input_fingerprint


def test_aggregation_failure_does_not_repeat_provider(db, client, flow, monkeypatch, tmp_path):
    import app.evaluation.speaking_qwen_worker as worker

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 2)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    provider = Provider()
    for _ in range(2):
        work = claim(db, ids[0], lease=timedelta(minutes=1))
        assert execute(db, *work, store, derived, provider)
    original_validate = worker.validate_result
    monkeypatch.setattr(
        worker,
        "validate_result",
        lambda *args: (_ for _ in ()).throw(ValueError("offline aggregation failure")),
    )
    with pytest.raises(ValueError):
        aggregate(db, ids[0])
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "WAITING"
        assert session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0])) is None
    monkeypatch.setattr(worker, "validate_result", original_validate)
    assert aggregate(db, ids[0]) and provider.calls == 2


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_original_preparation_has_no_dispatch(
    db, client, flow, monkeypatch, tmp_path, corrupt
):
    from app.media.audio_inspection import AudioError

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        from app.sessions.submission import Submission

        recording = session.get(Submission, job.submission_id).inputs["segments"][0]
    original = store.path(recording["storage_ref"], recording["sha256"])
    if corrupt:
        original.write_bytes(b"corrupt")
    else:
        original.unlink()
    with pytest.raises(AudioError, match="AUDIO_DIGEST_MISMATCH" if corrupt else "AUDIO_MISSING"):
        prepare(db, ids[0], store, derived, tools)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert job.input_refs is None and job.status == "WAITING"
        assert session.scalar(select(SpeakingFormalASROperation)) is None


def test_missing_derived_audio_cannot_reach_provider(db, client, flow, monkeypatch, tmp_path):
    import test_speaking_process_asr
    from test_audio_preparation import make_wav

    monkeypatch.setattr(test_speaking_process_asr, "wav", lambda: make_wav(rate=8000))
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
    with Session(db) as session:
        audio = session.get(SpeakingFormalASROperation, operation).audio_input
    derived.path(audio["storage_ref"], audio["sha256"]).unlink()
    provider = Provider()
    assert execute(db, operation, token, store, derived, provider)
    assert provider.calls == 0
    with Session(db) as session:
        assert session.get(SpeakingFormalASROperation, operation).error_code == "AUDIO_UNAVAILABLE"


def test_vendor_audio_limit_is_technical_failure_without_dispatch(
    db, client, flow, monkeypatch, tmp_path
):
    import app.evaluation.speaking_qwen_worker as worker

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    monkeypatch.setattr(worker, "MAX_SECONDS", 0.001)
    with pytest.raises(KnownFailure, match="ASR_AUDIO_INCOMPATIBLE"):
        prepare(db, ids[0], store, derived, tools)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).input_refs is None
        assert session.scalar(select(SpeakingFormalASROperation)) is None


def test_base64_limit_is_enforced_before_freeze(db, client, flow, monkeypatch, tmp_path):
    import app.evaluation.speaking_qwen_worker as worker

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    monkeypatch.setattr(worker, "MAX_DATA_URI_BYTES", 30)
    with pytest.raises(KnownFailure, match="ASR_AUDIO_INCOMPATIBLE"):
        prepare(db, ids[0], store, derived, tools)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).input_refs is None
        assert session.scalar(select(SpeakingFormalASROperation)) is None


def test_redo_prevents_old_job_dispatch(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, base, headers, old_attempt, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_attempt},
    )
    assert redo.status_code == 200
    provider = Provider()
    assert not execute(db, operation, token, store, derived, provider)
    assert provider.calls == 0
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "SKIPPED"
        assert session.get(SpeakingFormalASROperation, operation).status == "WAITING"


def test_response_before_commit_crash_stays_unknown(db, client, flow, monkeypatch, tmp_path):
    import app.evaluation.speaking_qwen_worker as worker

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    op, token = claim(db, ids[0], lease=timedelta(minutes=1))
    provider = Provider()
    original_finish = worker._finish_operation
    monkeypatch.setattr(
        worker,
        "_finish_operation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated process crash")),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        execute(db, op, token, store, derived, provider)
    assert provider.calls == 1
    monkeypatch.setattr(worker, "_finish_operation", original_finish)
    with Session(db) as session, session.begin():
        row = session.get(SpeakingFormalASROperation, op)
        row.lease_until = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        assert recover_expired(session) == 1
        assert session.get(SpeakingFormalASROperation, op).status == "UNKNOWN"
        assert session.get(EvaluationJob, ids[0]).status == "UNKNOWN"
    assert not aggregate(db, ids[0])
    assert claim(db, ids[0], lease=timedelta(minutes=1)) is None


def test_actual_dashscope_sdk_transport_is_intercepted_in_full_chain(
    db, client, flow, monkeypatch, tmp_path
):
    import dashscope
    import httpx
    from test_dashscope_asr import body

    from app.evaluation.speaking_qwen_adapter import SpeakingQwenASR

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 2)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=body(f"mock-{len(calls)}"))

    http = httpx.Client(transport=httpx.MockTransport(handle))
    provider = SpeakingQwenASR(
        client_factory=lambda **kwargs: dashscope.Client(**kwargs, http_client=http),
        key_provider=lambda: "offline-test-only",
    )
    for _ in range(2):
        operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
        assert execute(db, operation, token, store, derived, provider)
    assert len(calls) == 2 and aggregate(db, ids[0])
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert [segment["text"] for segment in raw.payload["segments"]] == ["mock-1", "mock-2"]


def test_confirmed_segment_cannot_be_changed_or_deleted(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert prepare(db, ids[0], store, derived, tools)
    operation, token = claim(db, ids[0], lease=timedelta(minutes=1))
    assert execute(db, operation, token, store, derived, Provider())
    for sql in (
        "UPDATE speaking_formal_asr_operations SET status='WAITING', result=NULL WHERE operation_id=:id",
        "DELETE FROM speaking_formal_asr_operations WHERE operation_id=:id",
        "UPDATE speaking_formal_asr_attempts SET status='FAILED' WHERE execution_id=:id",
    ):
        identifier = token if "attempts" in sql else operation
        with pytest.raises(DBAPIError), db.begin() as connection:
            connection.execute(text(sql), {"id": identifier})
