"""Speaking frozen source identities, consumption contracts and evaluation recovery."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_speaking_process_asr import REQUIREMENT, FakeASR, ready, segment
from test_teacher_api import AUTH

from app.api.assessment_repetition import get_recording_store
from app.api.teacher_review import get_scoring_registry
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    AdapterResult,
    InputIdentityConflict,
    InputSnapshot,
    authorize_retry,
    claim_job,
    execute_claim,
    run_once,
    schedule,
    speaking_source,
)
from app.main import app
from app.papers.paper import PaperEntry
from app.scoring.registry import EvaluationContract, Registry
from app.sessions.submission import Submission
from app.task_types.speaking_asr_worker import claim as claim_asr
from app.task_types.speaking_asr_worker import execute as execute_asr
from app.taskbank.models import TaskRevision

pytest_plugins = ("test_speaking_process_asr",)


def valid_consumption(source, consumption, requirement, upstream, payload):
    return (
        set(consumption) == {"segment_ids"}
        and isinstance(consumption["segment_ids"], list)
        and bool(consumption["segment_ids"])
        and len(set(consumption["segment_ids"])) == len(consumption["segment_ids"])
        and set(consumption["segment_ids"]).issubset({x["segment_id"] for x in source["segments"]})
        and (upstream is None or upstream["speaking_source_sha256"] == source["sha256"])
        and (payload is None or payload.get("fixture") == "offline")
    )


class OfflineAdapter:
    def __init__(self, *, missing=False, failure=False):
        self.missing, self.failure, self.calls = missing, failure, 0

    def resolve_inputs(self, session, submission, revision, requirement):
        if self.failure:
            raise ValueError("private diagnostic must not escape")
        refs = (
            {}
            if self.missing
            else {
                "speaking_consumption": {
                    "segment_ids": [submission.inputs["segments"][0]["segment_id"]]
                }
            }
        )
        return InputSnapshot(refs=refs, payload={"fixture": "offline"})

    def evaluate(self, inputs, requirement):
        self.calls += 1
        return AdapterResult(payload={"status": "TEXT", "text": "offline"}, provider="offline")

    def validate_result(self, result, requirement):
        return result.payload.get("text") == "offline"


def make_case(db, client, flow, monkeypatch, count, requirements=None, contracts=None):
    import test_speaking_checkpoint_a as fixtures

    reqs = requirements or [{"source": "offline-speaking", "version": "1", "parameters": {}}]
    if contracts is None:
        contracts = [
            EvaluationContract(
                "offline-speaking",
                "1",
                frozenset({"speaking"}),
                lambda p: p == {},
                speaking_consumption_validator=valid_consumption,
            )
        ]

    class Requirements(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, tuple(reqs) if key == "speaking" else value)

    class Plugin(fixtures.TestPlugin):
        def __init__(self):
            super().__init__()
            self.evaluation_requirements = Requirements(self.evaluation_requirements)

    monkeypatch.setattr(fixtures, "TestPlugin", Plugin)
    monkeypatch.setattr(fixtures, "Registry", lambda plugins: Registry(plugins, contracts))
    root, base, headers, aid = ready(client, flow)
    assert (
        client.put(
            "/teacher/assessment/speaking-policy",
            headers=AUTH,
            json={"minimum_duration_seconds": 0.001},
        ).status_code
        == 200
    )
    for i in range(count):
        operation, _ = segment(client, root, base, headers, aid, f"record-{i}")
        assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    response = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": aid, "finish_request_id": "finish"},
    )
    assert response.status_code == 200
    submission_id = UUID(response.json()["submission_id"])
    registry = app.dependency_overrides[get_scoring_registry]()
    store = app.dependency_overrides[get_recording_store]()
    with Session(db) as session, session.begin():
        assert schedule(session) == len(reqs)
    with Session(db) as session:
        jobs = list(
            session.scalars(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == submission_id)
                .order_by(EvaluationJob.requirement_index)
            )
        )
        ids = [job.job_id for job in jobs]
    return root, base, headers, aid, submission_id, registry, store, ids


def claim(db, job_id, registry, store, adapter, source="offline-speaking"):
    return claim_job(
        db,
        job_id,
        registry=registry,
        adapters={(source, "1"): adapter},
        lease=timedelta(minutes=2),
        recording_store=store,
    )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_full_source_anchors_all_segments_and_subset_consumption(
    db, client, flow, monkeypatch, count
):
    import hashlib

    from app.evaluation.worker import _json

    _, _, _, _, sid, registry, store, ids = make_case(db, client, flow, monkeypatch, count)
    adapter = OfflineAdapter()
    work = claim(db, ids[0], registry, store, adapter)
    assert work and len(work.inputs.refs["speaking_consumption"]["segment_ids"]) == 1
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        source = job.input_refs["speaking_source"]
        frozen = session.get(Submission, sid).inputs["segments"]
        assert [x["segment_id"] for x in source["segments"]] == [x["segment_id"] for x in frozen]
        assert len(source["segments"]) == count and len(source["sha256"]) == 64
        assert [x["sha256"] for x in source["segments"]] == [x["sha256"] for x in frozen]
        for index in range(count):
            changed = deepcopy(source)
            expected = changed.pop("sha256")
            changed["segments"][index]["sha256"] = "0" * 64
            assert hashlib.sha256(_json(changed).encode()).hexdigest() != expected
        if count > 1:
            changed = deepcopy(source)
            expected = changed.pop("sha256")
            changed["segments"].reverse()
            assert hashlib.sha256(_json(changed).encode()).hexdigest() != expected
    assert execute_claim(db, work) and adapter.calls == 1
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[0]))
        assert job.status == "READY" and raw.submission_id == sid
        assert (
            raw.input_refs["speaking_source"] == source
            and raw.input_fingerprint == job.input_fingerprint
        )


def test_missing_consumption_and_retry_source_integrity(db, client, flow, monkeypatch):
    _, _, _, _, sid, registry, store, ids = make_case(db, client, flow, monkeypatch, 2)
    adapter = OfflineAdapter(missing=True)
    with pytest.raises(InputIdentityConflict):
        claim(db, ids[0], registry, store, adapter)
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert (
            job.status == "WAITING"
            and job.error_code == "INPUT_IDENTITY_CONFLICT"
            and job.input_refs is None
        )
        submission = session.get(Submission, sid)
        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        for mutation in ("reverse", "drop", "recording", "attempt", "sha", "storage"):
            inputs = deepcopy(submission.inputs)
            attempt_id = submission.attempt_id
            if mutation == "reverse":
                inputs["segments"].reverse()
            if mutation == "drop":
                inputs["segments"].pop()
            if mutation == "recording":
                inputs["segments"][0]["recording_id"] = str(uuid4())
            if mutation == "attempt":
                attempt_id = uuid4()
            if mutation == "sha":
                inputs["segments"][0]["sha256"] = "0" * 64
            if mutation == "storage":
                inputs["segments"][0]["storage_ref"] = "bad"
            fake = SimpleNamespace(
                inputs=inputs,
                attempt_id=attempt_id,
                session_task_id=submission.session_task_id,
                submission_id=sid,
            )
            with pytest.raises(InputIdentityConflict):
                speaking_source(session, fake, entry, revision, store=store)


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_missing_or_modified_file_blocks_before_external_call(
    db, client, flow, monkeypatch, damage
):
    _, _, _, _, sid, registry, store, ids = make_case(db, client, flow, monkeypatch, 2)
    with Session(db) as session:
        frozen = session.get(Submission, sid).inputs["segments"][1]
        path = store.root / frozen["storage_ref"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"tampered")
    adapter = OfflineAdapter()
    with pytest.raises(InputIdentityConflict):
        claim(db, ids[0], registry, store, adapter)
    assert adapter.calls == 0
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).error_code == "INPUT_IDENTITY_CONFLICT"
        assert session.scalar(select(RawEvaluation)) is None


def test_changed_or_missing_file_between_claim_and_dispatch_is_not_sent(
    db, client, flow, monkeypatch
):
    _, _, _, _, sid, registry, store, ids = make_case(db, client, flow, monkeypatch, 2)
    adapter = OfflineAdapter()
    work = claim(db, ids[0], registry, store, adapter)
    with Session(db) as session:
        frozen = session.get(Submission, sid).inputs["segments"][1]
        (store.root / frozen["storage_ref"]).unlink()
    assert not execute_claim(db, work) and adapter.calls == 0
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert job.status == "WAITING" and job.error_code == "INPUT_IDENTITY_CONFLICT"
        assert session.scalar(select(RawEvaluation)) is None


def test_retry_cannot_change_consumption_or_fingerprint(db, client, flow, monkeypatch):
    from app.evaluation.worker import KnownFailure

    _, _, _, _, sid, registry, store, ids = make_case(db, client, flow, monkeypatch, 2)

    class FailFirst(OfflineAdapter):
        def evaluate(self, inputs, requirement):
            self.calls += 1
            if self.calls == 1:
                raise KnownFailure("OFFLINE_FAILURE")
            return AdapterResult(payload={"status": "TEXT", "text": "offline"}, provider="offline")

    adapter = FailFirst()
    work = claim(db, ids[0], registry, store, adapter)
    assert execute_claim(db, work)
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[0])
        original_refs, fingerprint = deepcopy(job.input_refs), job.input_fingerprint
        assert job.status == "FAILED"
        authorize_retry(session, ids[0])

    class Changed(OfflineAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            return InputSnapshot(
                refs={
                    "speaking_consumption": {
                        "segment_ids": [submission.inputs["segments"][1]["segment_id"]]
                    }
                },
                payload={"fixture": "offline"},
            )

    with pytest.raises(InputIdentityConflict):
        claim(db, ids[0], registry, store, Changed())
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[0])
        assert job.input_refs == original_refs and job.input_fingerprint == fingerprint
    work = claim(db, ids[0], registry, store, adapter)
    assert execute_claim(db, work) and adapter.calls == 2
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "READY"
        assert (
            session.scalar(
                select(RawEvaluation).where(RawEvaluation.status == "READY")
            ).submission_id
            == sid
        )


def test_missing_adapter_and_bad_resolution_do_not_block_other_job(db, client, flow, monkeypatch):
    reqs = [
        {"source": name, "version": "1", "parameters": {}} for name in ("bad", "good", "absent")
    ]
    contracts = [
        EvaluationContract(
            name,
            "1",
            frozenset({"speaking"}),
            lambda p: p == {},
            speaking_consumption_validator=valid_consumption,
        )
        for name in ("bad", "good", "absent")
    ]
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, reqs, contracts
    )
    good = OfflineAdapter()
    result = run_once(
        db,
        registry=registry,
        adapters={("bad", "1"): OfflineAdapter(failure=True), ("good", "1"): good},
        lease=timedelta(minutes=2),
        batch_size=10,
        recording_store=store,
    )
    assert result["claimed"] == result["finished"] == good.calls == 1
    with Session(db) as session:
        bad, good_job, absent = (session.get(EvaluationJob, i) for i in ids)
        assert bad.status == "WAITING" and bad.error_code == "INPUT_RESOLUTION_FAILED"
        assert good_job.status == "READY"
        assert absent.status == "WAITING" and absent.error_code == "EVALUATION_ADAPTER_UNAVAILABLE"


def test_formal_asr_dependency_carries_complete_source_and_consumption(
    db, client, flow, monkeypatch
):
    asr = {"source": "offline-asr", "version": "1", "parameters": {}}
    downstream = {
        "source": "offline-text",
        "version": "1",
        "parameters": {},
        "depends_on": {"requirement_index": 0, "source": "offline-asr", "version": "1"},
    }
    contracts = [
        EvaluationContract(
            "offline-asr",
            "1",
            frozenset({"speaking"}),
            lambda p: p == {},
            recognition_result=lambda payload: {
                "status": payload["status"],
                "text": payload["text"],
            },
            speaking_consumption_validator=valid_consumption,
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
    _, _, _, _, sid, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, [asr, downstream], contracts
    )
    first, second = OfflineAdapter(), OfflineAdapter()
    assert execute_claim(db, claim(db, ids[0], registry, store, first, "offline-asr"))
    work = claim(db, ids[1], registry, store, second, "offline-text")
    assert (
        work.inputs.refs["asr"]["speaking_consumption"] == work.inputs.refs["speaking_consumption"]
    )
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert (
            work.inputs.refs["asr"]["speaking_source_sha256"]
            == job.input_refs["speaking_source"]["sha256"]
        )
        assert len(job.input_refs["speaking_source"]["segments"]) == 2
    assert execute_claim(db, work)
    with Session(db) as session:
        assert (
            session.scalar(
                select(RawEvaluation).where(RawEvaluation.job_id == ids[1])
            ).submission_id
            == sid
        )
        from app.papers.paper import EvaluationPlan
        from app.scoring.settlement import _evidence

        submission = session.get(Submission, sid)
        entry = session.get(PaperEntry, session.get(EvaluationJob, ids[1]).paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        assert (
            len(
                _evidence(
                    session,
                    submission,
                    entry,
                    revision,
                    session.get(EvaluationPlan, entry.paper_entry_id),
                    registry,
                )
            )
            == 2
        )


def test_contract_rejects_payload_mismatch_before_call(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = make_case(db, client, flow, monkeypatch, 1)

    class BadPayload(OfflineAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            result = super().resolve_inputs(session, submission, revision, requirement)
            result.payload["fixture"] = "other-input"
            return result

    adapter = BadPayload()
    with pytest.raises(InputIdentityConflict):
        claim(db, ids[0], registry, store, adapter)
    assert adapter.calls == 0


@pytest.mark.parametrize("damage", [False, True])
def test_contract_can_consume_derived_audio_with_separate_store(
    db, client, flow, monkeypatch, tmp_path, damage
):
    import hashlib

    from test_qa_http import wav

    from app.media.recording_store import RecordingStore

    def validator(source, consumption, requirement, upstream, payload):
        return (
            set(consumption) == {"segment_ids", "derived_recordings"}
            and len(consumption["segment_ids"]) == 1
            and consumption["segment_ids"][0] in {s["segment_id"] for s in source["segments"]}
            and len(consumption["derived_recordings"]) == 1
            and (payload is None or payload.get("fixture") == "offline")
        )

    contract = EvaluationContract(
        "offline-speaking",
        "1",
        frozenset({"speaking"}),
        lambda p: p == {},
        speaking_consumption_validator=validator,
    )
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2, contracts=[contract]
    )
    derived = RecordingStore(tmp_path / "derived")
    data = wav()
    digest = hashlib.sha256(data).hexdigest()
    stream, staged = derived.staging_file()
    with stream:
        stream.write(data)
    ref = derived.commit(staged, digest)
    staged.unlink()

    class DerivedAdapter(OfflineAdapter):
        def resolve_inputs(self, session, submission, revision, requirement):
            return InputSnapshot(
                refs={
                    "speaking_consumption": {
                        "segment_ids": [submission.inputs["segments"][0]["segment_id"]],
                        "derived_recordings": [
                            {"storage_ref": ref, "sha256": digest, "media_type": "audio/wav"}
                        ],
                    }
                },
                payload={"fixture": "offline"},
            )

    adapter = DerivedAdapter()
    work = claim_job(
        db,
        ids[0],
        registry=registry,
        adapters={("offline-speaking", "1"): adapter},
        lease=timedelta(minutes=2),
        recording_store=store,
        derived_recording_store=derived,
    )
    assert work
    if damage:
        (derived.root / ref).write_bytes(b"corrupted")
        assert not execute_claim(db, work) and adapter.calls == 0
        with Session(db) as session:
            assert session.get(EvaluationJob, ids[0]).error_code == "INPUT_IDENTITY_CONFLICT"
    else:
        assert execute_claim(db, work) and adapter.calls == 1
        with Session(db) as session:
            job = session.get(EvaluationJob, ids[0])
            assert len(job.input_refs["speaking_source"]["segments"]) == 2
            assert (
                job.input_refs["dependencies"]["speaking_consumption"]["derived_recordings"][0][
                    "sha256"
                ]
                == digest
            )


def test_redo_skips_old_job_and_new_source_stays_separate(db, client, flow, monkeypatch):
    root, base, headers, old_aid, old_submission, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 2
    )
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_aid},
    )
    assert redo.status_code == 200
    new_aid = redo.json()["attempt_id"]
    adapter = OfflineAdapter()
    assert claim(db, ids[0], registry, store, adapter) is None
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).status == "SKIPPED"
    prompt = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": new_aid, "request_id": "initial", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + "/playbacks/" + prompt + "/finish",
            headers=headers,
            json={"attempt_id": new_aid, "completed": True},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, new_aid, "new-record")
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    response = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": new_aid, "finish_request_id": "new-finish"},
    )
    assert response.status_code == 200
    new_submission = UUID(response.json()["submission_id"])
    with Session(db) as session, session.begin():
        assert schedule(session) == 1
    with Session(db) as session:
        new_job = session.scalar(
            select(EvaluationJob).where(EvaluationJob.submission_id == new_submission)
        )
        new_id = new_job.job_id
        old_ids = {
            s["segment_id"] for s in session.get(Submission, old_submission).inputs["segments"]
        }
        actual_new = session.get(Submission, new_submission)
        forged = SimpleNamespace(
            submission_id=old_submission,
            attempt_id=actual_new.attempt_id,
            session_task_id=actual_new.session_task_id,
            inputs=actual_new.inputs,
        )
        entry = session.get(PaperEntry, new_job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        with pytest.raises(InputIdentityConflict):
            speaking_source(session, forged, entry, revision, store=store)
    work = claim(db, new_id, registry, store, adapter)
    assert execute_claim(db, work)
    with Session(db) as session:
        source = session.get(EvaluationJob, new_id).input_refs["speaking_source"]
        assert source["attempt_id"] == new_aid and source["submission_id"] == str(new_submission)
        assert not old_ids.intersection({s["segment_id"] for s in source["segments"]})
        assert (
            session.scalar(
                select(RawEvaluation).where(RawEvaluation.job_id == new_id)
            ).submission_id
            == new_submission
        )


def test_speaking_contract_without_consumption_validator_is_fail_closed(
    db, client, flow, monkeypatch
):
    contract = EvaluationContract(
        "offline-speaking", "1", frozenset({"speaking"}), lambda p: p == {}
    )
    _, _, _, _, _, registry, store, ids = make_case(
        db, client, flow, monkeypatch, 1, contracts=[contract]
    )
    adapter = OfflineAdapter()
    with pytest.raises(InputIdentityConflict):
        claim(db, ids[0], registry, store, adapter)
    assert adapter.calls == 0
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[0]).error_code == "INPUT_IDENTITY_CONFLICT"
        assert session.scalar(select(RawEvaluation)) is None


def test_global_invalid_lease_is_reported_before_job_scan():
    with pytest.raises(ValueError, match="Lease must be positive"):
        run_once(None, registry=Registry(), adapters={}, lease=timedelta(0), batch_size=1)
