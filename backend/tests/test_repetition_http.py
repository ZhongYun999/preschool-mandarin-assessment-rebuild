"""Repetition HTTP, durable recording evidence, and evaluation integration."""

import io
import wave
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_assessment_http import access, create_body, issue
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG, TestPlugin
from test_taskbank import CONTENT, write
from test_teacher_api import AUTH

from app.api.assessment_repetition import get_recording_store
from app.api.teacher_api import get_audio_store
from app.api.teacher_review import get_scoring_registry
from app.main import app
from app.media.fixed_audio import AudioStore, prepare_audio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.registry import EvaluationContract, Registry
from app.sessions.submission import Submission

pytest_plugins = ("test_teacher_api",)


class RepetitionPlugin(TestPlugin):
    def __init__(self, required=False):
        super().__init__()
        self.evaluation_requirements["repetition"] = (
            ({"source": "fake-ise", "version": "1", "parameters": {}},) if required else ()
        )

    def score_task(self, task_context, raw_evaluations, submission_facts):
        return {"score": 1}

    def score_paper(self, paper_context, task_scores):
        return {"total": sum(x["score"]["score"] for x in task_scores)}


def wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 100)
    return buf.getvalue()


@pytest.fixture
def repetition(db, bank, client, tmp_path):
    def make(required=False):
        plugin = RepetitionPlugin(required)
        contract = EvaluationContract(
            "fake-ise", "1", frozenset({"repetition"}), lambda p: p == {}, False
        )
        registry = Registry([plugin], [contract] if required else [])
        tasks = [
            write(
                db,
                bank,
                replace(
                    CONTENT,
                    task_type="repetition",
                    guide_text=guide,
                    reference_answer="frozen target",
                    scoring_instruction="secret N",
                    ai_followup_scope="secret O",
                ),
                code=str(i),
            )
            for i, guide in enumerate((None, "guide"))
        ]
        paper = draft(db, bank)
        with Session(db) as s, s.begin():
            for i, task in enumerate(tasks):
                add_entry(
                    s, paper_id=paper, expected_version=i, task_id=task.task_id, position=i + 1
                )
            select_plugin(
                s,
                paper_id=paper,
                expected_version=2,
                plugin_id="test-only",
                version="1",
                registry=registry,
            )
            publish(
                s,
                paper_id=paper,
                expected_version=3,
                registry=registry,
                audio_config=TEST_AUDIO_CONFIG,
            )
        fixed = AudioStore(tmp_path / "fixed")
        recordings = RecordingStore(tmp_path / "recordings")
        prepare_audio(db, paper, fixed, Synth())
        app.dependency_overrides[get_audio_store] = lambda: fixed
        app.dependency_overrides[get_recording_store] = lambda: recordings
        app.dependency_overrides[get_scoring_registry] = lambda: registry
        response = client.post(
            "/teacher/assessment/sessions", headers=AUTH, json=create_body(paper)
        )
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        headers = access(issue(client, sid))
        root = f"/assessment/sessions/{sid}"
        tasks = client.get(root, headers=headers).json()["tasks"]
        return {
            "sid": sid,
            "paper_id": paper,
            "root": root,
            "tasks": tasks,
            "headers": headers,
            "store": recordings,
            "registry": registry,
            "fixed": fixed,
        }

    return make


def task_base(f, index=0):
    return f"{f['root']}/tasks/{f['tasks'][index]['session_task_id']}"


def start_ready(client, f, index=0, replay=0):
    base, h = task_base(f, index), f["headers"]
    content = client.get(base + "/content", headers=h)
    assert (
        content.status_code == 200
        and "frozen target" not in content.text
        and "secret N" not in content.text
    )
    attempt = client.post(base + "/attempt", headers=h).json()["attempt_id"]
    for kind in (["GUIDE"] if index == 1 else []) + ["INITIAL_PROMPT"] + ["USER_REPLAY"] * replay:
        req = kind + str(uuid4())
        row = client.post(
            base + "/playbacks",
            headers=h,
            json={"attempt_id": attempt, "request_id": req, "kind": kind},
        )
        assert row.status_code == 200, row.text
        finish = client.post(
            base + "/playbacks/" + row.json()["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": attempt, "completed": True},
        )
        assert finish.status_code == 200
    return attempt


def start_rec(client, f, attempt, index=0, request="record"):
    return client.post(
        task_base(f, index) + "/recordings",
        headers=f["headers"],
        json={"attempt_id": attempt, "request_id": request},
    )


def upload(client, f, attempt, rid, data=None, index=0):
    return client.post(
        task_base(f, index) + "/recordings/" + rid + "/content",
        headers={**f["headers"], "X-Attempt-Id": attempt, "Content-Type": "audio/wav"},
        content=wav() if data is None else data,
    )


def submit_http(client, f, attempt, rid, index=0, request="submit"):
    return client.post(
        task_base(f, index) + "/submissions/repetition",
        headers=f["headers"],
        json={"attempt_id": attempt, "recording_id": rid, "submit_request_id": request},
    )


def test_repetition_http_recording_and_recovery(db, client, repetition):
    f = repetition()
    base, headers = task_base(f), f["headers"]
    aid = start_ready(client, f, replay=2)
    row = start_rec(client, f, aid)
    assert row.status_code == 200
    rid = row.json()["recording_id"]
    assert start_rec(client, f, aid).json() == row.json()
    state = client.get(f["root"], headers=headers).json()["tasks"][0]
    assert state["recording_in_progress"] and {"UPLOAD_RECORDING", "CANCEL_RECORDING"} <= set(
        state["allowed_actions"]
    )
    assert submit_http(client, f, aid, rid).status_code == 409
    assert (
        client.post(
            base + "/playbacks",
            headers=headers,
            json={"attempt_id": aid, "request_id": "busy", "kind": "USER_REPLAY"},
        ).status_code
        == 409
    )
    cancel = client.post(
        base + "/recordings/" + rid + "/cancel", headers=headers, json={"attempt_id": aid}
    )
    assert cancel.status_code == 200 and cancel.json()["status"] == "CANCELLED"
    assert (
        client.post(
            base + "/recordings/" + rid + "/cancel", headers=headers, json={"attempt_id": aid}
        ).status_code
        == 200
    )
    rid = start_rec(client, f, aid, request="retry").json()["recording_id"]
    saved = upload(client, f, aid, rid)
    assert saved.status_code == 200 and saved.json()["status"] == "SAVED"
    assert upload(client, f, aid, rid).json() == saved.json()
    assert upload(client, f, aid, rid, b"different").status_code == 409
    assert client.get(saved.json()["content_url"], headers=headers).content == wav()
    state = client.get(f["root"], headers=headers).json()["tasks"][0]
    assert not state["recording_in_progress"] and state["saved_recording_ids"] == [rid]
    assert (
        "SUBMIT_REPETITION" in state["allowed_actions"]
        and "START_RECORDING" in state["allowed_actions"]
    )
    result = submit_http(client, f, aid, rid)
    assert result.status_code == 200
    assert submit_http(client, f, aid, rid).json() == result.json()
    with Session(db) as s:
        submission = s.get(Submission, UUID(result.json()["submission_id"]))
        assert submission.inputs["recording_id"] == rid
        assert len(submission.inputs["replay_playback_ids"]) == 2
    assert start_ready(client, f, index=1)


def test_file_integrity_and_transaction_rollback(db, client, repetition, monkeypatch):
    from sqlalchemy import event

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]

    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE RECORDINGS"):
            raise RuntimeError("injected rollback")

    event.listen(db, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="injected rollback"):
            upload(client, f, aid, rid)
    finally:
        event.remove(db, "before_cursor_execute", fail_update)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"
    assert upload(client, f, aid, rid).status_code == 200
    with Session(db) as s:
        row = s.get(Recording, UUID(rid))
        path = f["store"].path(row.storage_ref, row.sha256)
    path.write_bytes(b"corrupted")
    assert submit_http(client, f, aid, rid).status_code == 409
    assert (
        client.get(
            task_base(f) + "/recordings/" + rid + "/content", headers=f["headers"]
        ).status_code
        == 404
    )
    path.unlink()
    assert submit_http(client, f, aid, rid).status_code == 409
    with Session(db) as s:
        assert s.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None


def test_redo_isolates_old_recording_and_close(db, client, repetition):
    from app.sessions.close_manifest import CloseManifestEntry

    f = repetition()
    aid = start_ready(client, f, replay=1)
    rid = start_rec(client, f, aid).json()["recording_id"]
    assert upload(client, f, aid, rid).status_code == 200
    old = submit_http(client, f, aid, rid).json()["submission_id"]
    base = task_base(f)
    redo = client.post(base + "/redo", headers=f["headers"], json={"redo_request_id": "redo"})
    new = redo.json()["attempt_id"]
    assert upload(client, f, aid, rid).status_code == 409
    assert submit_http(client, f, new, rid, request="new").status_code == 409
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "c"}
        ).status_code
        == 409
    )
    for kind in ["INITIAL_PROMPT"]:
        p = client.post(
            base + "/playbacks",
            headers=f["headers"],
            json={"attempt_id": new, "request_id": kind, "kind": kind},
        ).json()
        assert (
            client.post(
                base + "/playbacks/" + p["playback_id"] + "/finish",
                headers=f["headers"],
                json={"attempt_id": new, "completed": True},
            ).status_code
            == 200
        )
    new_rec = start_rec(client, f, new).json()["recording_id"]
    assert upload(client, f, new, new_rec).status_code == 200
    accepted = submit_http(client, f, new, new_rec, request="new").json()["submission_id"]
    second_aid = start_ready(client, f, index=1)
    second_rec = start_rec(client, f, second_aid, index=1).json()["recording_id"]
    assert upload(client, f, second_aid, second_rec, index=1).status_code == 200
    assert submit_http(client, f, second_aid, second_rec, index=1).status_code == 200
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "c"}
        ).status_code
        == 200
    )
    assert start_rec(client, f, new, request="after").status_code == 409
    assert submit_http(client, f, new, new_rec, request="new").json()["submission_id"] == accepted
    with Session(db) as s:
        chosen = {str(x) for x in s.scalars(select(CloseManifestEntry.submission_id))}
        assert old not in chosen and accepted in chosen
        result = s.get(Submission, UUID(accepted))
        assert result.inputs["replay_playback_ids"] == []


@pytest.mark.parametrize("interrupt", ["redo", "revoke"])
def test_late_upload_is_rejected(db, client, repetition, monkeypatch, interrupt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    entered, release = Event(), Event()
    commit = f["store"].commit

    def held(path, digest):
        ref = commit(path, digest)
        entered.set()
        assert release.wait(10)
        return ref

    monkeypatch.setattr(f["store"], "commit", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload, client, f, aid, rid)
        try:
            assert entered.wait(10)
            if interrupt == "redo":
                result = client.post(
                    task_base(f) + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
                )
            else:
                result = client.post(
                    f"/teacher/assessment/sessions/{f['sid']}/access-tokens/revoke-all",
                    headers=AUTH,
                )
            assert result.status_code == 200
        finally:
            release.set()
        assert future.result(timeout=10).status_code == (409 if interrupt == "redo" else 401)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"


def test_cross_session_recording_isolation_and_closed(db, client, repetition):
    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    saved = upload(client, f, aid, rid).json()
    other = client.post(
        "/teacher/assessment/sessions",
        headers=AUTH,
        json={
            **create_body(f["paper_id"]),
            "create_request_id": "other",
        },
    )
    sid = other.json()["session_id"]
    other_h = access(issue(client, sid))
    assert client.get(saved["content_url"], headers=other_h).status_code == 401
    other_state = client.get(f"/assessment/sessions/{sid}", headers=other_h).json()
    other_base = f"/assessment/sessions/{sid}/tasks/{other_state['tasks'][0]['session_task_id']}"
    assert (
        client.get(other_base + "/recordings/" + rid + "/content", headers=other_h).status_code
        == 404
    )


@pytest.mark.parametrize("required", [False, True])
def test_repetition_evaluation_and_formal_result(db, client, repetition, required):
    from datetime import timedelta

    from sqlalchemy import func

    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.worker import AdapterResult, InputSnapshot, run_once
    from app.scoring.models import FinalScoreResult
    from app.scoring.worker import run_settlement_once

    f = repetition(required=required)
    for index in (0, 1):
        attempt = start_ready(client, f, index=index)
        rid = start_rec(client, f, attempt, index=index).json()["recording_id"]
        assert upload(client, f, attempt, rid, index=index).status_code == 200
        assert submit_http(client, f, attempt, rid, index=index).status_code == 200
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )

    class Adapter:
        calls = 0

        def resolve_inputs(self, session, submission, revision, requirement):
            row = session.get(Recording, UUID(submission.inputs["recording_id"]))
            assert row.attempt_id == submission.attempt_id and row.status == "SAVED"
            return InputSnapshot(
                refs={"recording_id": str(row.recording_id), "sha256": row.sha256},
                payload={
                    "ref": row.storage_ref,
                    "sha": row.sha256,
                    "target": revision.reference_answer,
                },
            )

        def evaluate(self, inputs, requirement):
            self.calls += 1
            assert inputs.payload["target"] == "frozen target"
            assert f["store"].read(inputs.payload["ref"], inputs.payload["sha"]) == wav()
            return AdapterResult(payload={"raw_score": 82.5})

        def validate_result(self, result, requirement):
            return result.payload.get("raw_score") == 82.5

    adapter = Adapter()
    out = run_once(
        db,
        registry=f["registry"],
        adapters={("fake-ise", "1"): adapter},
        lease=timedelta(minutes=1),
        batch_size=20,
    )
    assert out["finished"] == (2 if required else 0)
    assert adapter.calls == (2 if required else 0)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == (2 if required else 0)
        assert s.scalar(select(func.count()).select_from(RawEvaluation)) == (2 if required else 0)
    result = run_settlement_once(db, registry=f["registry"])
    assert len(result["completed"]) == 1 and not result["failures"]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 1
    assert client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH).json()["ready"]


def test_database_recording_history_is_immutable(db, client, repetition):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    assert upload(client, f, aid, rid).status_code == 200
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as c:
        c.execute(
            text("UPDATE recordings SET sha256=repeat('a',64) WHERE recording_id=:id"),
            {"id": UUID(rid)},
        )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(DBAPIError, match="recording facts must be preserved"):
            command.downgrade(config, "0017_credential_revocation")
        connection.rollback()
    assert (
        client.get(task_base(f) + "/recordings", headers=f["headers"]).json()[0]["status"]
        == "SAVED"
    )


def test_playback_recording_mutual_exclusion_and_rerecording(db, client, repetition):
    f = repetition()
    aid = start_ready(client, f)
    base, h = task_base(f), f["headers"]
    play = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "play", "kind": "USER_REPLAY"},
    ).json()
    assert start_rec(client, f, aid).status_code == 409
    assert (
        client.post(
            base + "/playbacks/" + play["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    first = start_rec(client, f, aid).json()["recording_id"]
    assert upload(client, f, aid, first).status_code == 200
    second = start_rec(client, f, aid, request="rerecord").json()["recording_id"]
    assert upload(client, f, aid, second, wav() + b"\0\0").status_code == 200
    response = submit_http(client, f, aid, second)
    assert response.status_code == 200
    assert submit_http(client, f, aid, first).status_code == 409
    with Session(db) as s:
        row = s.get(Submission, UUID(response.json()["submission_id"]))
        assert row.inputs["recording_id"] == second and row.inputs["replay_playback_ids"] == []


@pytest.mark.parametrize(
    "operation,first",
    [("submit", "submit"), ("submit", "close"), ("redo", "redo"), ("redo", "close")],
)
def test_recorded_submission_and_close_compete(
    db, client, repetition, monkeypatch, operation, first
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    import app.api.assessment_operations as common_api
    import app.api.assessment_repetition as repetition_api
    from app.sessions.close_manifest import CloseManifest, CloseManifestEntry, IncompleteSession

    f = repetition()
    ids = []
    for index in (0, 1):
        aid = start_ready(client, f, index=index)
        rid = start_rec(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        ids.append((aid, rid))
    first_submission = submit_http(client, f, *ids[0], index=0).json()["submission_id"]
    if operation == "redo":
        assert submit_http(client, f, *ids[1], index=1).status_code == 200
    target = repetition_api if first == "submit" else common_api
    name = {"submit": "submit_repetition", "redo": "redo_attempt", "close": "close_session"}[first]
    original = getattr(target, name)
    entered, release, competing = Event(), Event(), Event()

    def held(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except IncompleteSession:
            entered.set()
            assert release.wait(10)
            raise
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(target, name, held)

    def call(action):
        if action == "submit":
            return submit_http(client, f, *ids[1], index=1)
        if action == "redo":
            return client.post(
                task_base(f, 1) + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
            )
        return client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        )

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "assessment_sessions" in statement and "FOR UPDATE" in statement:
            competing.set()

    second = "close" if first != "close" else operation
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(call, first)
        assert entered.wait(10)
        event.listen(db, "before_cursor_execute", observe)
        try:
            b = pool.submit(call, second)
            assert competing.wait(10)
        finally:
            event.remove(db, "before_cursor_execute", observe)
            release.set()
        responses = {first: a.result(timeout=10), second: b.result(timeout=10)}
    if operation == "submit":
        assert responses["submit"].status_code == 200
        assert responses["close"].status_code == (200 if first == "submit" else 409)
    else:
        assert responses["redo"].status_code == (200 if first == "redo" else 409)
        assert responses["close"].status_code == (409 if first == "redo" else 200)
    with Session(db) as s:
        manifest = s.scalar(select(CloseManifest).where(CloseManifest.session_id == UUID(f["sid"])))
        if responses["close"].status_code == 200:
            assert manifest is not None
            chosen = {
                str(x)
                for x in s.scalars(
                    select(CloseManifestEntry.submission_id).where(
                        CloseManifestEntry.manifest_id == manifest.manifest_id
                    )
                )
            }
            assert first_submission in chosen
            if operation == "submit":
                assert responses["submit"].json()["submission_id"] in chosen
        else:
            assert manifest is None


def test_recording_and_playback_cannot_start_together(db, client, repetition):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.sessions.submission import Playback

    f = repetition()
    aid = start_ready(client, f)
    barrier = Barrier(2)

    def record():
        barrier.wait()
        return start_rec(client, f, aid)

    def play():
        barrier.wait()
        return client.post(
            task_base(f) + "/playbacks",
            headers=f["headers"],
            json={"attempt_id": aid, "request_id": "race", "kind": "USER_REPLAY"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(record), pool.submit(play)
        results = [a.result(timeout=10).status_code, b.result(timeout=10).status_code]
    assert sorted(results) == [200, 409]
    with Session(db) as s:
        recording = s.scalar(
            select(Recording).where(
                Recording.attempt_id == UUID(aid), Recording.status == "STARTED"
            )
        )
        playback = s.scalar(
            select(Playback).where(Playback.attempt_id == UUID(aid), Playback.status == "STARTED")
        )
        assert (recording is not None) != (playback is not None)


def test_missing_evidence_does_not_create_ready_evaluation(db, client, repetition):
    from datetime import timedelta

    from app.evaluation.models import RawEvaluation
    from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure, run_once
    from app.scoring.models import FinalScoreResult
    from app.scoring.worker import run_settlement_once

    f = repetition(required=True)
    missing_id = None
    for index in (0, 1):
        aid = start_ready(client, f, index=index)
        rid = start_rec(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        assert submit_http(client, f, aid, rid, index=index).status_code == 200
        if index == 0:
            missing_id = UUID(rid)
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "c"}
        ).status_code
        == 200
    )
    with Session(db) as s:
        row = s.get(Recording, missing_id)
        f["store"].path(row.storage_ref, row.sha256).unlink()

    class Adapter:
        def resolve_inputs(self, session, submission, revision, requirement):
            row = session.get(Recording, UUID(submission.inputs["recording_id"]))
            return InputSnapshot(
                refs={"recording_id": str(row.recording_id)},
                payload={"ref": row.storage_ref, "sha": row.sha256},
            )

        def evaluate(self, inputs, requirement):
            try:
                f["store"].verify(inputs.payload["ref"], inputs.payload["sha"])
            except ValueError as exc:
                raise KnownFailure("RECORDING_UNAVAILABLE") from exc
            return AdapterResult(payload={"raw_score": 1})

        def validate_result(self, result, requirement):
            return True

    run_once(
        db,
        registry=f["registry"],
        adapters={("fake-ise", "1"): Adapter()},
        lease=timedelta(minutes=1),
        batch_size=20,
    )
    out = run_settlement_once(db, registry=f["registry"])
    assert len(out["pending"]) == 1 and not out["completed"]
    with Session(db) as s:
        assert s.scalar(select(FinalScoreResult)) is None
        rows = list(s.scalars(select(RawEvaluation)))
        assert [row.status for row in rows] == ["FAILED", "FAILED"]
        assert all(row.payload is None for row in rows)


def test_concurrent_uploads_same_operation_do_not_overwrite(db, client, repetition, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    barrier = Barrier(2)
    commit = f["store"].commit

    def synchronized(path, digest):
        ref = commit(path, digest)
        barrier.wait(timeout=10)
        return ref

    monkeypatch.setattr(f["store"], "commit", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(upload, client, f, aid, rid, wav())
        b = pool.submit(upload, client, f, aid, rid, wav() + b"\0\0")
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sorted(r.status_code for r in results) == [200, 409]
    response = client.get(task_base(f) + "/recordings/" + rid + "/content", headers=f["headers"])
    assert response.content in (wav(), wav() + b"\0\0")
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "SAVED"


def test_upload_does_not_hold_business_lock_and_unfinished_cannot_submit(
    db, client, repetition, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    entered, release = Event(), Event()
    commit = f["store"].commit

    def hold(path, digest):
        ref = commit(path, digest)
        entered.set()
        assert release.wait(10)
        return ref

    monkeypatch.setattr(f["store"], "commit", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(upload, client, f, aid, rid)
        try:
            assert entered.wait(10)
            assert submit_http(client, f, aid, rid).status_code == 409
        finally:
            release.set()
        assert work.result(timeout=10).status_code == 200
    assert submit_http(client, f, aid, rid).status_code == 200


def test_recording_media_type_and_empty_upload(client, repetition):
    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    url = task_base(f) + "/recordings/" + rid + "/content"
    headers = {**f["headers"], "X-Attempt-Id": aid}
    assert (
        client.post(
            url, headers={**headers, "Content-Type": "video/mp4"}, content=b"data"
        ).status_code
        == 422
    )
    assert (
        client.post(url, headers={**headers, "Content-Type": "audio/wav"}, content=b"").status_code
        == 422
    )
    result = client.post(
        url, headers={**headers, "Content-Type": "audio/ogg"}, content=b"controlled-test-data"
    )
    assert result.status_code == 200 and result.json()["media_type"] == "audio/ogg"
    assert (
        client.post(
            url, headers={**headers, "Content-Type": "audio/wav"}, content=b"controlled-test-data"
        ).status_code
        == 409
    )


def test_repetition_technical_retry_is_not_replay(db, client, repetition):
    f = repetition()
    base, h = task_base(f), f["headers"]
    aid = client.post(base + "/attempt", headers=h).json()["attempt_id"]
    prompt = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "initial", "kind": "INITIAL_PROMPT"},
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + prompt["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    retry = client.post(
        base + "/playbacks",
        headers=h,
        json={
            "attempt_id": aid,
            "request_id": "retry",
            "kind": "TECHNICAL_RETRY",
            "retry_of_id": prompt["playback_id"],
        },
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + retry["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = start_rec(client, f, aid).json()["recording_id"]
    saved = upload(client, f, aid, rid)
    assert saved.status_code == 200
    assert "sha256" not in saved.json() and "storage_ref" not in saved.json()
    submitted = submit_http(client, f, aid, rid)
    assert submitted.status_code == 200
    with Session(db) as s:
        assert (
            s.get(Submission, UUID(submitted.json()["submission_id"])).inputs["replay_playback_ids"]
            == []
        )


def test_saved_recording_survives_submission_transaction_failure(db, client, repetition):
    from sqlalchemy import event

    f = repetition()
    aid = start_ready(client, f)
    rid = start_rec(client, f, aid).json()["recording_id"]
    assert upload(client, f, aid, rid).status_code == 200

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SUBMISSIONS"):
            raise RuntimeError("injected submission failure")

    event.listen(db, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="injected submission failure"):
            submit_http(client, f, aid, rid)
    finally:
        event.remove(db, "before_cursor_execute", fail_insert)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "SAVED"
        assert s.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None
    assert submit_http(client, f, aid, rid).status_code == 200
