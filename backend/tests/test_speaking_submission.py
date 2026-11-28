"""Speaking submission, runtime policy and transactional recovery."""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_speaking_guidance import CONTRACT, FakeGuidance, help_request, simulate_delivery
from test_speaking_process_asr import REQUIREMENT, FakeASR, ready, segment
from test_teacher_api import AUTH

from app.sessions.submission import EvaluationIntent, Submission
from app.task_types.speaking_asr_worker import claim as claim_asr
from app.task_types.speaking_asr_worker import execute as execute_asr
from app.task_types.speaking_guidance_worker import claim, execute

pytest_plugins = ("test_speaking_process_asr",)


def policy(client, seconds):
    return client.put(
        "/teacher/assessment/speaking-policy",
        headers=AUTH,
        json={"minimum_duration_seconds": seconds},
    )


def finish(client, base, headers, aid, request_id="finish"):
    return client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": aid, "finish_request_id": request_id},
    )


def recognize(db, client, root, base, headers, aid, request_id, mode="TEXT"):
    operation, item = segment(client, root, base, headers, aid, request_id)
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR(mode))
    return item


def test_policy_auth_and_dynamic_threshold(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    state = client.get(root, headers=headers).json()["tasks"][0]["speaking"]
    assert (
        state["minimum_duration_seconds"] is None
        and state["finish_unavailable_reason"] == "SPEAKING_POLICY_UNAVAILABLE"
    )
    assert (
        client.put(
            "/teacher/assessment/speaking-policy",
            headers=headers,
            json={"minimum_duration_seconds": 1},
        ).status_code
        == 401
    )
    assert policy(client, 0).status_code == 422
    assert policy(client, 1).status_code == 200
    recognize(db, client, root, base, headers, aid, "one", "NO_TEXT")
    state = client.get(root, headers=headers).json()["tasks"][0]
    duration = state["speaking"]["total_duration_seconds"]
    assert 0 < duration < 1 and not state["speaking"]["minimum_duration_met"]
    assert finish(client, base, headers, aid).status_code == 409
    assert policy(client, duration).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert (
        state["speaking"]["minimum_duration_met"] and "FINISH_SPEAKING" in state["allowed_actions"]
    )
    assert (
        "START_RECORDING" in state["allowed_actions"]
        and "REQUEST_HELP" in state["allowed_actions"]
        and "START_USER_REPLAY" in state["allowed_actions"]
    )
    assert policy(client, duration * 2).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert (
        not state["speaking"]["minimum_duration_met"]
        and "FINISH_SPEAKING" not in state["allowed_actions"]
    )
    assert len(state["speaking"]["segments"]) == 1
    assert finish(client, base, headers, aid).status_code == 409


def test_manifest_frozen_counts_and_idempotence(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    first = recognize(db, client, root, base, headers, aid, "one", "NO_TEXT")
    second = recognize(db, client, root, base, headers, aid, "two")
    help_id = UUID(help_request(client, base, headers, aid, "help").json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    assert finish(client, base, headers, aid).status_code == 409
    simulate_delivery(db, root, base, aid, help_id)
    voided_id = UUID(help_request(client, base, headers, aid, "voided").json()["help_request_id"])
    assert execute(db, claim(db, voided_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    simulate_delivery(db, root, base, aid, voided_id, void=True)
    replay = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": aid, "request_id": "replay", "kind": "USER_REPLAY"},
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + replay["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    retry = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "retry",
            "kind": "TECHNICAL_RETRY",
            "retry_of_id": replay["playback_id"],
        },
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + retry["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    response = finish(client, base, headers, aid)
    assert response.status_code == 200 and response.json()["status"] == "ACCEPTED"
    submission_id = UUID(response.json()["submission_id"])
    with Session(db) as session:
        saved = session.get(Submission, submission_id)
        manifest = dict(saved.inputs)
        assert [item["segment_id"] for item in manifest["segments"]] == [
            first["segment_id"],
            second["segment_id"],
        ]
        assert [item["position"] for item in manifest["segments"]] == [1, 2]
        assert all(
            item["sha256"] and item["storage_ref"] and item["asr_status"] == "READY"
            for item in manifest["segments"]
        )
        assert manifest["effective_help_count"] == 1 and manifest["delivered_help"][0][
            "help_request_id"
        ] == str(help_id)
        assert manifest["replay_count"] == 1 and manifest["replay_playback_ids"] == [
            replay["playback_id"]
        ]
        intents = list(
            session.scalars(
                select(EvaluationIntent).where(EvaluationIntent.submission_id == submission_id)
            )
        )
        assert intents == []
    assert policy(client, 1000).status_code == 200
    assert finish(client, base, headers, aid).json()["submission_id"] == str(submission_id)
    assert finish(client, base, headers, aid, "different").status_code == 409
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["attempt_status"] == "SEALED" and state["speaking"]["submission_id"] == str(
        submission_id
    )
    assert (
        "FINISH_SPEAKING" not in state["allowed_actions"]
        and "START_RECORDING" not in state["allowed_actions"]
    )
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": aid},
    )
    assert redo.status_code == 200
    assert finish(client, base, headers, aid).json()["submission_id"] == str(submission_id)
    assert finish(client, base, headers, aid, "stale").status_code == 409
    with Session(db) as session:
        assert session.get(Submission, submission_id).inputs == manifest


@pytest.mark.parametrize(
    "competition", ["same", "different", "record", "help", "replay", "redo", "close"]
)
def test_submission_competing_operations(db, client, flow, competition):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    recognize(db, client, root, base, headers, aid, "recorded")
    barrier = Barrier(2)

    def submission():
        barrier.wait(timeout=10)
        return finish(client, base, headers, aid)

    def other():
        barrier.wait(timeout=10)
        if competition == "same":
            return finish(client, base, headers, aid)
        if competition == "different":
            return finish(client, base, headers, aid, "another")
        if competition == "record":
            return client.post(
                base + "/recordings",
                headers=headers,
                json={"attempt_id": aid, "request_id": "next"},
            )
        if competition == "help":
            return help_request(client, base, headers, aid, "next")
        if competition == "replay":
            return client.post(
                base + "/playbacks",
                headers=headers,
                json={"attempt_id": aid, "request_id": "replay", "kind": "USER_REPLAY"},
            )
        if competition == "redo":
            return client.post(
                base + "/redo",
                headers=headers,
                json={"redo_request_id": "redo", "expected_attempt_id": aid},
            )
        return client.post(root + "/close", headers=headers, json={"close_request_id": "close"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(submission), pool.submit(other)
        result, competing = a.result(timeout=15), b.result(timeout=15)
    if competition == "same":
        assert result.status_code == competing.status_code == 200
        assert result.json()["submission_id"] == competing.json()["submission_id"]
    elif competition == "redo":
        assert competing.status_code == 200 and result.status_code in (200, 409)
    elif competition == "close":
        assert result.status_code == 200 and competing.status_code in (200, 409)
        if competing.status_code == 200:
            assert finish(client, base, headers, aid).status_code == 200
            assert finish(client, base, headers, aid, "late").status_code == 409
    else:
        assert sorted((result.status_code, competing.status_code)) == [200, 409]
    with Session(db) as session:
        rows = list(session.scalars(select(Submission).where(Submission.attempt_id == UUID(aid))))
        assert len(rows) <= 1


def test_missing_audio_rejects_submission_without_sealing(db, client, flow):
    from app.api.assessment_repetition import get_recording_store
    from app.main import app
    from app.media.recording import Recording

    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    item = recognize(db, client, root, base, headers, aid, "record")
    with Session(db) as session:
        record = session.get(Recording, UUID(item["recording_id"]))
        (app.dependency_overrides[get_recording_store]().root / record.storage_ref).unlink()
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert (
        not state["speaking"]["finish_eligible"]
        and state["speaking"]["finish_unavailable_reason"] == "RECORDING_UNAVAILABLE"
    )
    assert finish(client, base, headers, aid).status_code == 409
    with Session(db) as session:
        assert session.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None


def test_pending_asr_and_recording_block_finish(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    operation, _ = segment(client, root, base, headers, aid, "pending")
    assert finish(client, base, headers, aid).status_code == 409
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    recording = client.post(
        base + "/recordings", headers=headers, json={"attempt_id": aid, "request_id": "active"}
    )
    assert recording.status_code == 200 and finish(client, base, headers, aid).status_code == 409
    assert (
        client.post(
            base + "/recordings/" + recording.json()["recording_id"] + "/cancel",
            headers=headers,
            json={"attempt_id": aid},
        ).status_code
        == 200
    )
    assert finish(client, base, headers, aid).status_code == 200


def test_submission_transaction_rolls_back_all_facts(db, client, flow):
    from app.api.assessment_repetition import get_recording_store
    from app.main import app
    from app.sessions.assessment_session import Attempt
    from app.task_types.speaking import submit_speaking

    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    recognize(db, client, root, base, headers, aid, "record")
    sid, tid = UUID(root.rsplit("/", 1)[1]), UUID(base.rsplit("/", 1)[1])
    store = app.dependency_overrides[get_recording_store]()
    with pytest.raises(RuntimeError), Session(db) as session, session.begin():
        submit_speaking(
            session,
            session_id=sid,
            session_task_id=tid,
            attempt_id=UUID(aid),
            finish_request_id="rollback",
            store=store,
        )
        raise RuntimeError("simulate transaction failure")
    with Session(db) as session:
        assert session.get(Attempt, UUID(aid)).status == "OPEN"
        assert session.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None
        assert session.scalar(select(EvaluationIntent)) is None
    assert finish(client, base, headers, aid, "rollback").status_code == 200


def test_policy_change_and_submission_use_one_atomic_policy_version(db, client, flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    recognize(db, client, root, base, headers, aid, "record")
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=10)
        return finish(client, base, headers, aid)

    def change():
        barrier.wait(timeout=10)
        return policy(client, 100.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(submit), pool.submit(change)
        result, updated = a.result(timeout=15), b.result(timeout=15)
    assert updated.status_code == 200
    assert result.status_code in (200, 409)
    if result.status_code == 200:
        with Session(db) as session:
            inputs = session.get(Submission, UUID(result.json()["submission_id"])).inputs
            assert inputs["minimum_duration_seconds"] == 0.001 and inputs["policy_revision"] == 1
    else:
        with Session(db) as session:
            assert (
                session.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None
            )


def test_frozen_plan_creates_single_intent_and_job(db, client, flow, monkeypatch):
    import test_speaking_checkpoint_a as fixtures

    from app.api.assessment_repetition import get_recording_store
    from app.evaluation.models import EvaluationJob
    from app.evaluation.worker import schedule
    from app.main import app
    from app.papers.paper import EvaluationPlan
    from app.scoring.registry import EvaluationContract, Registry
    from app.task_types.speaking import submit_speaking

    requirement = {"source": "fake-speaking", "version": "1", "parameters": {}}

    class Requirements(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, (requirement,) if key == "speaking" else value)

    original_plugin = fixtures.TestPlugin

    class Plugin(original_plugin):
        def __init__(self):
            super().__init__()
            self.evaluation_requirements = Requirements(self.evaluation_requirements)

    monkeypatch.setattr(fixtures, "TestPlugin", Plugin)
    monkeypatch.setattr(
        fixtures,
        "Registry",
        lambda plugins: Registry(
            plugins,
            [
                EvaluationContract(
                    "fake-speaking",
                    "1",
                    frozenset({"speaking"}),
                    lambda params: params == {},
                    speaking_consumption_validator=lambda *args: True,
                )
            ],
        ),
    )
    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    recognize(db, client, root, base, headers, aid, "record")
    with pytest.raises(RuntimeError), Session(db) as session, session.begin():
        submit_speaking(
            session,
            session_id=UUID(root.rsplit("/", 1)[1]),
            session_task_id=UUID(base.rsplit("/", 1)[1]),
            attempt_id=UUID(aid),
            finish_request_id="rollback",
            store=app.dependency_overrides[get_recording_store](),
        )
        assert len(list(session.scalars(select(EvaluationIntent)))) == 1
        raise RuntimeError("rollback")
    with Session(db) as session:
        assert session.scalar(select(Submission)) is None
        assert session.scalar(select(EvaluationIntent)) is None
    response = finish(client, base, headers, aid)
    assert response.status_code == 200
    submission_id = UUID(response.json()["submission_id"])
    with Session(db) as session, session.begin():
        submission = session.get(Submission, submission_id)
        plan = session.get(EvaluationPlan, UUID(submission.inputs["paper_entry_id"]))
        assert plan.requirements == [requirement]
        intents = list(
            session.scalars(
                select(EvaluationIntent).where(EvaluationIntent.submission_id == submission_id)
            )
        )
        assert len(intents) == 1 and intents[0].requirement_index == 0
        assert schedule(session) == 1
        assert schedule(session) == 0
        jobs = list(
            session.scalars(
                select(EvaluationJob).where(EvaluationJob.submission_id == submission_id)
            )
        )
        assert len(jobs) == 1 and jobs[0].requirement == requirement
    assert finish(client, base, headers, aid).json()["submission_id"] == str(submission_id)
    with Session(db) as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(EvaluationIntent).where(
                            EvaluationIntent.submission_id == submission_id
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("stage", ["saving", "asr", "guidance"])
def test_finish_racing_checkpoint_transitions(db, client, flow, stage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_qa_http import wav
    from test_speaking_guidance import make_audio

    root, base, headers, aid = ready(client, flow)
    assert policy(client, 0.001).status_code == 200
    recognize(db, client, root, base, headers, aid, "first")
    if stage == "saving":
        recording = client.post(
            base + "/recordings", headers=headers, json={"attempt_id": aid, "request_id": "next"}
        ).json()["recording_id"]

        def advance():
            return (
                client.post(
                    base + "/recordings/" + recording + "/content",
                    headers={**headers, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
                    content=wav(),
                ).status_code
                == 200
            )
    elif stage == "asr":
        operation, _ = segment(client, root, base, headers, aid, "next")
        work = claim_asr(db, operation, REQUIREMENT)

        def advance():
            return execute_asr(db, work, FakeASR())
    else:
        help_id = UUID(help_request(client, base, headers, aid, "help").json()["help_request_id"])
        assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
        make_audio(db, help_id)
        started = client.post(
            base + "/playbacks",
            headers=headers,
            json={
                "attempt_id": aid,
                "request_id": "guidance",
                "kind": "GUIDANCE",
                "help_request_id": str(help_id),
            },
        ).json()["playback_id"]

        def advance():
            return (
                client.post(
                    base + "/playbacks/" + started + "/finish",
                    headers=headers,
                    json={"attempt_id": aid, "completed": True},
                ).status_code
                == 200
            )

    barrier = Barrier(2)

    def completion():
        barrier.wait(timeout=10)
        return finish(client, base, headers, aid)

    def progression():
        barrier.wait(timeout=10)
        return advance()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(completion), pool.submit(progression)
        result, advanced = a.result(timeout=20), b.result(timeout=20)
    assert advanced
    assert result.status_code == 409 if stage == "saving" else result.status_code in (200, 409)
    if result.status_code == 200:
        with Session(db) as session:
            inputs = session.get(Submission, UUID(result.json()["submission_id"])).inputs
            assert len(inputs["segments"]) == (2 if stage == "asr" else 1)
            assert inputs["effective_help_count"] == (1 if stage == "guidance" else 0)
