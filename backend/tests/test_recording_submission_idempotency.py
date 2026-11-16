"""Idempotent recorded submissions across session lifecycle boundaries."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_teacher_api import AUTH

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import AdapterResult, InputSnapshot, run_once
from app.media.recording import Recording
from app.scoring.models import FinalScoreResult
from app.scoring.worker import run_settlement_once
from app.sessions.assessment_session import Attempt, SessionTask
from app.sessions.close_manifest import CloseManifestEntry
from app.sessions.submission import EvaluationIntent, Submission

pytest_plugins = ("test_qa_http", "test_repetition_http")


@pytest.mark.parametrize("task_type", ["qa", "repetition"])
def test_recorded_submission_retry_after_close_and_completion(db, client, request, task_type):
    if task_type == "qa":
        import test_qa_http as helpers

        f = request.getfixturevalue("qa_flow")(required=True)

        def ready(index=0):
            return helpers.deliver(client, f, index=index)

        def begin(aid, index=0):
            return helpers.record(client, f, aid, index=index).json()["recording_id"]

        def send(aid, rid, index=0):
            return helpers.upload(client, f, aid, rid, index=index)

        def submit(aid, rid, index=0, req="submit"):
            return helpers.submit(client, f, aid, rid, index=index, request=req)

        url = helpers.base(f)
        sources = ("test-text", "test-acoustic")
    else:
        import test_repetition_http as helpers

        f = request.getfixturevalue("repetition")(required=True)

        def ready(index=0):
            return helpers.start_ready(client, f, index=index)

        def begin(aid, index=0):
            return helpers.start_rec(client, f, aid, index=index).json()["recording_id"]

        def send(aid, rid, index=0):
            return helpers.upload(client, f, aid, rid, index=index)

        def submit(aid, rid, index=0, req="submit"):
            return helpers.submit_http(client, f, aid, rid, index=index, request=req)

        url = helpers.task_base(f)
        sources = ("fake-ise",)
    h = f["headers"]
    old_attempt = ready()
    old_recording = begin(old_attempt)
    assert send(old_attempt, old_recording).status_code == 200
    old = submit(old_attempt, old_recording, req="old")
    assert (
        old.status_code == 200
        and submit(old_attempt, old_recording, req="old").json() == old.json()
    )
    redo = client.post(url + "/redo", headers=h, json={"redo_request_id": "redo"}).json()
    new_attempt = redo["attempt_id"]
    assert submit(old_attempt, old_recording, req="old").json() == old.json()
    with Session(db) as s:
        task = s.get(SessionTask, UUID(f["tasks"][0]["session_task_id"]))
        assert str(task.current_attempt_id) == new_attempt
    prompt = client.post(
        url + "/playbacks",
        headers=h,
        json={"attempt_id": new_attempt, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    ).json()
    assert (
        client.post(
            url + "/playbacks/" + prompt["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": new_attempt, "completed": True},
        ).status_code
        == 200
    )
    new_recording = begin(new_attempt)
    assert send(new_attempt, new_recording).status_code == 200
    accepted = submit(new_attempt, new_recording, req="new")
    assert (
        accepted.status_code == 200
        and submit(new_attempt, new_recording, req="new").json() == accepted.json()
    )
    assert submit(new_attempt, old_recording, req="new").status_code == 409
    assert submit(new_attempt, new_recording, req="old").status_code == 409
    second_attempt = ready(index=1)
    second_recording = begin(second_attempt, index=1)
    assert send(second_attempt, second_recording, index=1).status_code == 200
    assert submit(second_attempt, second_recording, index=1).status_code == 200
    closed = client.post(f["root"] + "/close", headers=h, json={"close_request_id": "close"})
    assert closed.status_code == 200 and closed.json()["status"] == "INPUT_CLOSED"
    wrong_type = "repetition" if task_type == "qa" else "qa"
    wrong_url = url + "/submissions/" + wrong_type
    wrong_body = {
        "attempt_id": new_attempt,
        "recording_id": new_recording,
        "submit_request_id": "new",
    }
    assert client.post(wrong_url, headers=h, json=wrong_body).status_code == 409
    with Session(db) as s:
        selected = set(s.scalars(select(CloseManifestEntry.submission_id)))
        assert UUID(old.json()["submission_id"]) not in selected
        assert UUID(accepted.json()["submission_id"]) in selected

    def counts():
        with Session(db) as s:
            return tuple(
                s.scalar(select(func.count()).select_from(m))
                for m in (
                    Attempt,
                    Submission,
                    EvaluationIntent,
                    EvaluationJob,
                    RawEvaluation,
                    FinalScoreResult,
                )
            )

    before = counts()
    assert submit(new_attempt, new_recording, req="new").json() == accepted.json()
    assert submit(old_attempt, old_recording, req="old").json() == old.json()
    assert submit(new_attempt, old_recording, req="new").status_code == 409
    assert submit(new_attempt, new_recording, req="old").status_code == 409
    assert submit(new_attempt, new_recording, req="fresh").status_code == 409
    assert counts() == before

    class Adapter:
        calls = 0

        def resolve_inputs(self, session, submission, revision, requirement):
            row = session.get(Recording, UUID(submission.inputs["recording_id"]))
            assert row.attempt_id == submission.attempt_id
            return InputSnapshot(
                refs={"recording_id": str(row.recording_id)},
                payload={"ref": row.storage_ref, "sha": row.sha256},
            )

        def evaluate(self, inputs, requirement):
            self.calls += 1
            f["store"].verify(inputs.payload["ref"], inputs.payload["sha"])
            return AdapterResult(payload={"metric": 1})

        def validate_result(self, result, requirement):
            return True

    adapter = Adapter()
    evaluation = run_once(
        db,
        registry=f["registry"],
        adapters={(source, "1"): adapter for source in sources},
        lease=timedelta(minutes=1),
        batch_size=50,
    )
    assert evaluation["finished"] == 2 * len(sources)
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    assert client.get(f["root"], headers=h).json()["status"] == "COMPLETED"
    assert client.post(wrong_url, headers=h, json=wrong_body).status_code == 409
    before, calls = counts(), adapter.calls
    assert submit(new_attempt, new_recording, req="new").json() == accepted.json()
    assert submit(old_attempt, old_recording, req="old").json() == old.json()
    assert submit(new_attempt, old_recording, req="new").status_code == 409
    assert submit(new_attempt, new_recording, req="old").status_code == 409
    assert submit(new_attempt, new_recording, req="fresh").status_code == 409
    assert submit(new_attempt, str(uuid4()), req="fresh").status_code == 409
    assert counts() == before and adapter.calls == calls
    with Session(db) as s:
        task = s.get(SessionTask, UUID(f["tasks"][0]["session_task_id"]))
        assert str(task.current_attempt_id) == new_attempt
        assert s.get(Attempt, UUID(new_attempt)).status == "SEALED"
    assert (
        client.post(
            f"/teacher/assessment/sessions/{f['sid']}/access-tokens/revoke-all", headers=AUTH
        ).status_code
        == 200
    )
    assert submit(new_attempt, new_recording, req="new").status_code == 401
    assert counts() == before and adapter.calls == calls
