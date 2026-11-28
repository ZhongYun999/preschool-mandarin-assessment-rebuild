"""Offline formal ASR contracts; fixtures never call a model provider."""

from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_speaking_evaluation_input import OfflineAdapter, claim, make_case, valid_consumption
from test_speaking_process_asr import REQUIREMENT, FakeASR, segment

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.source_identity import speaking_source
from app.evaluation.speaking_asr_contract import SCHEMA, recognition_result
from app.evaluation.speaking_asr_contract import valid_consumption as asr_consumption
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    authorize_retry,
    execute_claim,
    schedule,
)
from app.papers.paper import PaperEntry
from app.scoring.registry import EvaluationContract
from app.sessions.assessment_session import SessionTask
from app.task_types.speaking_asr_worker import claim as claim_process_asr
from app.task_types.speaking_asr_worker import execute as execute_process_asr

pytest_plugins = ("test_speaking_process_asr",)

PARAMS = {
    "provider": "offline",
    "model": "fixture",
    "processing_version": "identity-v1",
    "confirmed_no_text": True,
}
ASR = {"source": "offline-multisegment-asr", "version": "1", "parameters": PARAMS}
TEXT = {
    "source": "offline-text",
    "version": "1",
    "parameters": {},
    "depends_on": {"requirement_index": 0, "source": ASR["source"], "version": "1"},
}
CONTRACTS = [
    EvaluationContract(
        ASR["source"],
        "1",
        frozenset({"speaking"}),
        lambda p: p == PARAMS,
        speaking_consumption_validator=asr_consumption,
        speaking_recognition_result=recognition_result,
    ),
    EvaluationContract(
        TEXT["source"],
        "1",
        frozenset({"speaking"}),
        lambda p: p == {},
        accepts_no_text=True,
        speaking_consumption_validator=valid_consumption,
    ),
]


class MultiASR:
    def __init__(self, mutation=None, no_text=False, subset=False, store=None):
        self.mutation, self.no_text = mutation, no_text
        self.subset = subset
        self.store = store

    def resolve_inputs(self, session, submission, revision, requirement):
        task = session.get(SessionTask, submission.session_task_id)
        entry = session.get(PaperEntry, task.paper_entry_id)
        source = speaking_source(session, submission, entry, revision)
        audio = [
            {
                "segment_id": s["segment_id"],
                "recording_id": s["recording_id"],
                "position": s["position"],
                "sha256": s["sha256"],
                "storage_ref": s["storage_ref"],
                "media_type": s["media_type"],
                "processing_version": PARAMS["processing_version"],
            }
            for s in source["segments"]
        ]
        if self.subset:
            audio = [audio[0], audio[-1]]
        return InputSnapshot(
            refs={
                "speaking_consumption": {
                    "segment_ids": [s["segment_id"] for s in audio],
                    "audio_inputs": audio,
                    "derived_recordings": [],
                }
            },
            payload={
                "audio_inputs": audio,
                "submission_id": source["submission_id"],
                "source_sha256": source["sha256"],
            },
        )

    def evaluate(self, inputs, requirement):
        for audio in inputs.payload["audio_inputs"]:
            assert self.store.read(audio["storage_ref"], audio["sha256"])
        segments = [
            {
                "segment_id": s["segment_id"],
                "recording_id": s["recording_id"],
                "position": s["position"],
                "input_sha256": s["sha256"],
                "processing_version": s["processing_version"],
                "status": "NO_TEXT" if self.no_text else "TEXT",
                "text": None if self.no_text else f"fixture-{i}",
            }
            for i, s in enumerate(inputs.payload["audio_inputs"])
        ]
        payload = {
            "schema": SCHEMA,
            "submission_id": inputs.payload["submission_id"],
            "speaking_source_sha256": inputs.payload["source_sha256"],
            "contract": {"source": requirement["source"], "version": requirement["version"]},
            "segments": segments,
        }
        if self.mutation:
            self.mutation(payload)
        return AdapterResult(payload=payload, provider="offline", model="fixture")

    def validate_result(self, result, requirement):
        return True


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("no_text", [False, True])
def test_formal_multisegment_handoff(db, client, flow, monkeypatch, count, no_text):
    _, _, _, _, sid, registry, store, ids = make_case(
        db, client, flow, monkeypatch, count, [ASR, TEXT], CONTRACTS
    )
    adapter = MultiASR(no_text=no_text, store=store)
    work = claim(db, ids[0], registry, store, adapter, ASR["source"])
    assert work and execute_claim(db, work)
    downstream = claim(db, ids[1], registry, store, OfflineAdapter(), TEXT["source"])
    assert downstream
    result = downstream.inputs.payload["asr"]
    assert result["source"] == "formal_speaking_asr"
    assert result["status"] == ("NO_TEXT" if no_text else "TEXT")
    assert [s["text"] for s in result["segments"]] == [
        None if no_text else f"fixture-{i}" for i in range(count)
    ]
    with Session(db) as session:
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        job = session.get(EvaluationJob, ids[0])
        assert raw.submission_id == sid and raw.status == "READY"
        assert raw.input_fingerprint == job.input_fingerprint
        assert downstream.inputs.refs["asr"]["evaluation_id"] == str(raw.evaluation_id)
        assert downstream.inputs.refs["asr"]["input_fingerprint"] == raw.input_fingerprint
    assert execute_claim(db, downstream)


def missing(p):
    p["segments"].pop()


def duplicate(p):
    p["segments"][1] = deepcopy(p["segments"][0])


def reordered(p):
    p["segments"].reverse()


def wrong_recording(p):
    p["segments"][0]["recording_id"] = "other-attempt"


def wrong_digest(p):
    p["speaking_source_sha256"] = "0" * 64


def wrong_version(p):
    p["schema"] = "speaking-asr-result-v2"


def wrong_contract(p):
    p["contract"]["version"] = "2"


def failed_as_empty(p):
    p["segments"][0].update(status="FAILED", text=None)


@pytest.mark.parametrize(
    "mutation",
    [
        missing,
        duplicate,
        reordered,
        wrong_recording,
        wrong_digest,
        wrong_version,
        wrong_contract,
        failed_as_empty,
    ],
)
def test_invalid_result_never_ready(db, client, flow, monkeypatch, mutation):
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, [ASR, TEXT], CONTRACTS
    )
    work = claim(db, ids[0], registry, store, MultiASR(mutation, store=store), ASR["source"])
    assert execute_claim(db, work)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "INVALID"
        assert not session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[0], RawEvaluation.status == "READY"
            )
        )
    assert claim(db, ids[1], registry, store, OfflineAdapter(), TEXT["source"]) is None


def test_technical_failure_and_retry_preserve_formal_input(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, [ASR, TEXT], CONTRACTS
    )

    class Failed(MultiASR):
        def evaluate(self, inputs, requirement):
            raise KnownFailure("OFFLINE_TECHNICAL_FAILURE")

    assert execute_claim(db, claim(db, ids[0], registry, store, Failed(store=store), ASR["source"]))
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[0])
        assert job.status == "FAILED"
        refs, fingerprint = deepcopy(job.input_refs), job.input_fingerprint
        authorize_retry(session, ids[0])
    work = claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    assert execute_claim(db, work)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert job.status == "READY"
        assert job.input_refs == refs and job.input_fingerprint == fingerprint


def test_subset_consumption_keeps_complete_source_identity(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 3, [ASR, TEXT], CONTRACTS
    )
    work = claim(db, ids[0], registry, store, MultiASR(subset=True, store=store), ASR["source"])
    assert work and execute_claim(db, work)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        source = job.input_refs["speaking_source"]
        consumed = job.input_refs["dependencies"]["speaking_consumption"]["segment_ids"]
        assert len(source["segments"]) == 3
        assert consumed == [
            source["segments"][0]["segment_id"],
            source["segments"][2]["segment_id"],
        ]
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert raw.payload["speaking_source_sha256"] == source["sha256"]


def test_redo_cannot_use_previous_formal_recognition(db, client, flow, monkeypatch):
    root, base, headers, old_attempt, old_submission, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, [ASR, TEXT], CONTRACTS
    )
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    response = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_attempt},
    )
    assert response.status_code == 200
    attempt = response.json()["attempt_id"]
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": attempt, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
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
    assert execute_process_asr(db, claim_process_asr(db, operation, REQUIREMENT), FakeASR())
    submission = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": attempt, "finish_request_id": "new-finish"},
    )
    assert submission.status_code == 200
    new_submission = UUID(submission.json()["submission_id"])
    with Session(db) as session, session.begin():
        assert schedule(session) == 2
        jobs = list(
            session.scalars(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == new_submission)
                .order_by(EvaluationJob.requirement_index)
            )
        )
        new_asr, new_text = [job.job_id for job in jobs]
        old_raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        old_raw_id = str(old_raw.evaluation_id)
        assert old_raw.submission_id == old_submission
    assert claim(db, new_text, registry, store, OfflineAdapter(), TEXT["source"]) is None
    assert execute_claim(
        db, claim(db, new_asr, registry, store, MultiASR(store=store), ASR["source"])
    )
    downstream = claim(db, new_text, registry, store, OfflineAdapter(), TEXT["source"])
    assert downstream.inputs.refs["asr"]["submission_id"] == str(new_submission)
    assert downstream.inputs.refs["asr"]["evaluation_id"] != old_raw_id
