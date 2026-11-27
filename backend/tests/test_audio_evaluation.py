"""Exercise prepared audio through the persisted ASR dependency pipeline."""

from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_asr_dependency import adapters, claim

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.source_identity import recording_input
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    claim_job,
    recover_expired,
    run_once,
)
from app.media.audio_inspection import AudioError, AudioTools
from app.media.audio_normalization import AudioSpec, prepare_audio
from app.media.recording_store import RecordingStore
from app.scoring.models import FinalScoreResult
from app.scoring.worker import run_settlement_once

pytest_plugins = ("test_asr_dependency",)


class PreparingAsr:
    def __init__(self, flow, spec, tools, derived, no_text=False):
        self.flow, self.spec, self.tools, self.derived, self.no_text = (
            flow,
            spec,
            tools,
            derived,
            no_text,
        )
        self.calls = 0

    def resolve_inputs(self, db, submission, revision, requirement):
        recording = recording_input(db, submission)
        return InputSnapshot(
            refs={
                **{key: recording[key] for key in ("recording_id", "sha256", "media_type")},
                "audio_spec": asdict(self.spec),
            },
            payload={"recording": recording},
        )

    def evaluate(self, inputs, requirement):
        self.calls += 1
        try:
            prepared = prepare_audio(
                inputs.payload["recording"],
                AudioSpec(**inputs.refs["audio_spec"]),
                self.flow["store"],
                self.derived,
                self.tools,
            )
        except AudioError as exc:
            raise KnownFailure(exc.code) from exc
        return AdapterResult(
            payload={
                "status": "NO_TEXT" if self.no_text else "TEXT",
                "text": None if self.no_text else "real words",
                "audio_evidence": asdict(prepared),
            }
        )

    def validate_result(self, result, requirement):
        return True


def setup_asr(f, tmp_path, no_text=False):
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 8000, 1, "test-v1", tools.version(), tools.version(probe=True)
    )
    asr = PreparingAsr(f, spec, tools, RecordingStore(tmp_path / "derived"), no_text)
    _, llm, acoustic, sources = adapters(f)
    sources[("test-asr", "1")] = asr
    return asr, llm, acoustic, sources


@pytest.mark.parametrize("no_text", [False, True])
def test_worker_audio_evidence_and_llm(db, dependency_flow, tmp_path, no_text):
    f = dependency_flow(no_text=no_text, allow_no_text=no_text)
    asr, llm, acoustic, sources = setup_asr(f, tmp_path, no_text)
    for _ in range(3):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    assert (asr.calls, llm.calls, acoustic.calls) == (1, 1, 1)
    assert llm.received[0].payload["asr"] == {
        "status": "NO_TEXT" if no_text else "TEXT",
        "text": None if no_text else "real words",
    }
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        evidence = raw.payload["audio_evidence"]
        assert evidence["recording_id"] == f["recording_id"]
        assert evidence["original_sha256"] == job.input_refs["dependencies"]["sha256"]
        assert evidence["spec"] == job.input_refs["dependencies"]["audio_spec"]
        assert evidence["output"]["sample_rate"] == 8000 and not evidence["reused_original"]
        assert evidence["output"]["sha256"] != evidence["original_sha256"]
        derived_hash = evidence["output"]["sha256"]
        derived_ref = evidence["storage_ref"]
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    db.dispose()
    fresh = RecordingStore(tmp_path / "derived")
    fresh.verify(derived_ref, derived_hash)
    for _ in range(2):
        assert (
            run_once(
                db,
                registry=f["registry"],
                adapters=sources,
                lease=timedelta(minutes=1),
                batch_size=10,
            )["claimed"]
            == 0
        )
        run_settlement_once(db, registry=f["registry"])
    with Session(db) as s:
        assert len(list(s.scalars(select(RawEvaluation)))) == 3
        assert len(list(s.scalars(select(FinalScoreResult)))) == 1


def test_bad_recording_fails_asr_but_not_independent_job(db, dependency_flow, tmp_path):
    f = dependency_flow()
    asr, llm, acoustic, sources = setup_asr(f, tmp_path)
    with Session(db) as s:
        from app.media.recording import Recording

        recording = s.get(Recording, f["recording_id"])
        path = f["store"].path(recording.storage_ref, recording.sha256)
    path.write_bytes(b"damaged")
    for _ in range(2):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    with Session(db) as s:
        asr_job = s.get(EvaluationJob, f["jobs"]["test-asr"])
        assert (asr_job.status, asr_job.error_code) == ("FAILED", "AUDIO_DIGEST_MISMATCH")
        assert s.get(EvaluationJob, f["jobs"]["test-llm"]).status == "WAITING"
        assert s.get(EvaluationJob, f["jobs"]["test-acoustic"]).status == "READY"
    assert (asr.calls, llm.calls, acoustic.calls) == (1, 0, 1)


def test_changed_audio_spec_rejected_on_reclaim(db, dependency_flow, tmp_path):
    f = dependency_flow()
    asr, _, _, sources = setup_asr(f, tmp_path)
    first = claim(db, f, "test-asr", sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        refs, fingerprint = job.input_refs, job.input_fingerprint
    with db.begin() as connection:
        connection.execute(
            text(
                "UPDATE evaluation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:id"
            ),
            {"id": first.job_id},
        )
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    asr.spec = replace(asr.spec, processing_version="changed")
    with pytest.raises(InputIdentityConflict):
        claim_job(
            db, first.job_id, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1)
        )
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        assert (job.input_refs, job.input_fingerprint) == (refs, fingerprint)
        assert s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == first.job_id)) is None
    assert asr.calls == 0
