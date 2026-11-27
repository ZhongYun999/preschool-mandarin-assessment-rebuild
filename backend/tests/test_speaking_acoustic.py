"""Deterministic signal measurements and PostgreSQL evaluation integration."""

import io
import math
import struct
import wave
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_speaking_evaluation_input import make_case

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.speaking_acoustic import measure, prepare_adapter, run_job
from app.evaluation.speaking_acoustic_contract import (
    ALGORITHM,
    SOURCE,
    VERSION,
    valid_consumption,
    valid_parameters,
)
from app.evaluation.worker import (
    InputIdentityConflict,
    KnownFailure,
    claim_job,
    execute_claim,
    recover_expired,
)
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.registry import PRODUCTION_REGISTRY, EvaluationContract

pytest_plugins = ("test_speaking_process_asr",)


def pcm(samples):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    return buffer.getvalue()


def params(tools):
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "acoustic-prep-v1", tools.version(), tools.version(probe=True)
    )
    return {
        "audio_spec": asdict(spec),
        "processing_version": spec.processing_version,
        "algorithm": ALGORITHM,
        "frame_samples": 320,
        "active_rms_ratio": 0.01,
        "long_gap_samples": 24000,
    }


@pytest.fixture
def config():
    return params(AudioTools())


def test_silence_tone_and_partial_frame(config):
    silent = measure(pcm([0] * 24001), config)
    assert silent["energy_active_duration_seconds"] == 0
    assert silent["mean_active_rms_dbfs"] is None
    assert silent["long_low_energy_gap_count"] == 1
    assert silent["long_low_energy_gap_duration_seconds"] == 24001 / 16000
    constant = measure(pcm([16384] * 321), config)
    assert (
        constant["audio_duration_seconds"]
        == constant["energy_active_duration_seconds"]
        == 321 / 16000
    )
    assert constant["energy_active_ratio"] == 1
    assert constant["mean_active_rms_dbfs"] == pytest.approx(-6.020599913)
    sine = measure(
        pcm([round(10000 * math.sin(2 * math.pi * i / 40)) for i in range(16000)]), config
    )
    assert sine["energy_active_ratio"] == 1
    assert sine["mean_active_rms_dbfs"] == pytest.approx(
        20 * math.log10(10000 / math.sqrt(2) / 32768), abs=0.001
    )


def test_known_gap_and_weighted_ratio(config):
    from app.evaluation.speaking_acoustic import aggregate

    first = measure(pcm([10000] * 320 + [0] * 24000 + [10000] * 320), config)
    second = measure(pcm([10000] * 16000), config)
    assert first["long_low_energy_gap_count"] == 1
    assert first["long_low_energy_gap_duration_seconds"] == 1.5
    result = aggregate([first, second])
    assert result["total_long_low_energy_gap_count"] == 1
    assert result["overall_energy_active_ratio"] == pytest.approx((640 + 16000) / (24640 + 16000))
    assert (
        result["overall_energy_active_ratio"]
        != (first["energy_active_ratio"] + second["energy_active_ratio"]) / 2
    )
    edges = aggregate([measure(pcm([0] * 16000), config)] * 2)
    assert edges["total_long_low_energy_gap_count"] == 0


def test_invalid_audio_and_algorithm(config):
    with pytest.raises(KnownFailure):
        measure(b"not a wav", config)
    assert valid_parameters(config)
    assert not valid_parameters({**config, "algorithm": "changed"})


def setup_case(db, client, flow, monkeypatch, tmp_path, count):
    tools = AudioTools()
    requirement = {"source": SOURCE, "version": VERSION, "parameters": params(tools)}
    assert PRODUCTION_REGISTRY.supports("speaking", requirement)
    assert not PRODUCTION_REGISTRY.supports("qa", requirement)
    contract = EvaluationContract(
        SOURCE,
        VERSION,
        frozenset({"speaking"}),
        valid_parameters,
        speaking_consumption_validator=valid_consumption,
    )
    case = make_case(db, client, flow, monkeypatch, count, [requirement], [contract])
    return case, tools, RecordingStore(tmp_path / "derived")


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_full_job_creates_one_traced_result(db, client, flow, monkeypatch, tmp_path, count):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, count)
    _, _, _, _, submission, _, store, ids = case
    assert run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    assert not run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        raws = list(session.scalars(select(RawEvaluation).where(RawEvaluation.job_id == ids[0])))
        assert job.status == "READY" and len(raws) == 1
        raw = raws[0]
        assert raw.submission_id == submission and raw.input_fingerprint == job.input_fingerprint
        assert len(raw.payload["segments"]) == count
        assert raw.payload["aggregate"]["segment_count"] == count
        assert [s["segment_id"] for s in raw.payload["segments"]] == [
            s["segment_id"] for s in raw.input_refs["speaking_source"]["segments"]
        ]
        assert (
            not {"final_score", "speech_clarity_fluency_score", "raw_task_score"}
            & raw.payload.keys()
        )


def test_original_damage_fails_without_ready(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    adapter = prepare_adapter(db, ids[0], store, derived, tools)
    source = adapter.source["segments"][0]
    store.path(source["storage_ref"], source["sha256"]).write_bytes(b"damaged")
    assert not run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert job.status == "WAITING" and job.error_code == "AUDIO_DIGEST_MISMATCH"
        assert session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0])) is None


def test_fingerprint_cannot_drift_after_claim(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    adapter = prepare_adapter(db, ids[0], store, derived, tools)
    claim = claim_job(
        db,
        ids[0],
        registry=PRODUCTION_REGISTRY,
        adapters={(SOURCE, VERSION): adapter},
        lease=timedelta(minutes=1),
        recording_store=store,
        derived_recording_store=derived,
    )
    assert claim is not None
    with pytest.raises(DBAPIError, match="immutable"), Session(db) as session, session.begin():
        session.get(EvaluationJob, ids[0]).input_fingerprint = "0" * 64


@pytest.mark.parametrize("damage", ["missing", "corrupted"])
def test_derived_audio_loss_blocks_recovery(db, client, flow, monkeypatch, tmp_path, damage):
    import test_speaking_process_asr as process

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<h", 4000) * 1600)
    monkeypatch.setattr(process, "wav", lambda: buffer.getvalue())
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    adapter = prepare_adapter(db, ids[0], store, derived, tools)
    item = adapter.consumption["derived_recordings"][0]
    old_claim = claim_job(
        db,
        ids[0],
        registry=PRODUCTION_REGISTRY,
        adapters={(SOURCE, VERSION): adapter},
        lease=timedelta(microseconds=1),
        recording_store=store,
        derived_recording_store=derived,
    )
    assert old_claim is not None
    with Session(db) as session, session.begin():
        assert recover_expired(session) == 1
    path = derived.path(item["storage_ref"], item["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt derived audio")
    with pytest.raises(InputIdentityConflict, match="derived audio unavailable"):
        run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "WAITING"
        assert session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0])) is None


def test_converted_audio_is_anchored_in_result(db, client, flow, monkeypatch, tmp_path):
    import test_speaking_process_asr as process

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<h", 4000) * 1600)
    monkeypatch.setattr(process, "wav", lambda: buffer.getvalue())
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    assert run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        source = raw.input_refs["speaking_source"]["segments"][0]
        consumed = raw.input_refs["dependencies"]["speaking_consumption"]["audio_inputs"][0]
        assert consumed["sha256"] != source["sha256"]
        assert raw.payload["segments"][0]["original_sha256"] == source["sha256"]
        assert raw.payload["segments"][0]["input_sha256"] == consumed["sha256"]
        derived.verify(consumed["storage_ref"], consumed["sha256"])


def test_input_closed_remains_evaluable(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    root, _, headers, _, submission, _, store, ids = case
    assert (
        client.post(
            root + "/close", headers=headers, json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    assert run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert raw.submission_id == submission and raw.status == "READY"


def test_redo_cannot_use_previous_job(db, client, flow, monkeypatch, tmp_path):
    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, base, headers, attempt, _, _, store, ids = case
    assert (
        client.post(
            base + "/redo",
            headers=headers,
            json={"redo_request_id": "redo", "expected_attempt_id": attempt},
        ).status_code
        == 200
    )
    assert not run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "SKIPPED"
        assert session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0])) is None


def test_redo_preserves_old_result_and_anchors_new_submission(
    db, client, flow, monkeypatch, tmp_path
):
    from uuid import UUID

    from test_speaking_process_asr import REQUIREMENT, FakeASR, segment

    from app.evaluation.worker import schedule
    from app.task_types.speaking_asr_worker import claim as claim_asr
    from app.task_types.speaking_asr_worker import execute as execute_asr

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    root, base, headers, old_attempt, old_submission, _, store, ids = case
    assert run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_attempt},
    )
    assert redo.status_code == 200
    attempt = redo.json()["attempt_id"]
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": attempt, "request_id": "initial", "kind": "INITIAL_PROMPT"},
    )
    assert playback.status_code == 200
    assert (
        client.post(
            base + "/playbacks/" + playback.json()["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": attempt, "completed": True},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, attempt, "new-record")
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    submitted = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": attempt, "finish_request_id": "new-finish"},
    )
    assert submitted.status_code == 200
    new_submission = UUID(submitted.json()["submission_id"])
    with Session(db) as session, session.begin():
        assert schedule(session) == 1
        new_id = session.scalar(
            select(EvaluationJob.job_id).where(EvaluationJob.submission_id == new_submission)
        )
    assert run_job(db, new_id, original_store=store, derived_store=derived, tools=tools)
    with Session(db) as session:
        old = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        new = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == new_id))
        assert old.submission_id == old_submission and new.submission_id == new_submission
        assert old.input_refs["speaking_source"]["attempt_id"] == old_attempt
        assert new.input_refs["speaking_source"]["attempt_id"] == attempt
        assert old.payload["segments"][0]["segment_id"] != new.payload["segments"][0]["segment_id"]


def test_expired_local_dispatch_recovers_without_paid_outcome(
    db, client, flow, monkeypatch, tmp_path
):
    from sqlalchemy import func

    case, tools, derived = setup_case(db, client, flow, monkeypatch, tmp_path, 1)
    _, _, _, _, _, _, store, ids = case
    adapter = prepare_adapter(db, ids[0], store, derived, tools)
    old_claim = claim_job(
        db,
        ids[0],
        registry=PRODUCTION_REGISTRY,
        adapters={(SOURCE, VERSION): adapter},
        lease=timedelta(minutes=1),
        recording_store=store,
        derived_recording_store=derived,
    )
    assert old_claim is not None
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[0])
        job.dispatch_started_at = session.scalar(select(func.clock_timestamp()))
        job.lease_until = job.dispatch_started_at - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        assert recover_expired(session) == 1
        assert session.get(EvaluationJob, ids[0]).status == "WAITING"
    assert not execute_claim(db, old_claim)
    assert run_job(db, ids[0], original_store=store, derived_store=derived, tools=tools)


def test_no_declared_requirement_creates_no_acoustic_job(db, client, flow):
    from test_speaking_process_asr import REQUIREMENT, FakeASR, ready, segment
    from test_teacher_api import AUTH

    from app.evaluation.worker import schedule
    from app.task_types.speaking_asr_worker import claim as claim_asr
    from app.task_types.speaking_asr_worker import execute as execute_asr

    root, base, headers, attempt = ready(client, flow)
    assert (
        client.put(
            "/teacher/assessment/speaking-policy",
            headers=AUTH,
            json={"minimum_duration_seconds": 0.001},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, attempt, "record")
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    assert (
        client.post(
            base + "/submissions/speaking",
            headers=headers,
            json={"attempt_id": attempt, "finish_request_id": "finish"},
        ).status_code
        == 200
    )
    with Session(db) as session, session.begin():
        assert schedule(session) == 0
        assert not list(session.scalars(select(EvaluationJob)))
