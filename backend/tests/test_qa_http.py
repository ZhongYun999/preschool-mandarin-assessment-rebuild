"""QA HTTP and independent evaluation sources using shared recording facts."""

import io
import wave
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
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
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.main import app
from app.media.fixed_audio import AudioStore, prepare_audio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.registry import EvaluationContract, Registry
from app.sessions.assessment_session import Attempt
from app.sessions.submission import EvaluationIntent, Playback, Submission

pytest_plugins = ("test_teacher_api",)


class QaPlugin(TestPlugin):
    def __init__(self, required):
        super().__init__()
        self.evaluation_requirements["qa"] = (
            (
                {"source": "test-text", "version": "1", "parameters": {}},
                {"source": "test-acoustic", "version": "1", "parameters": {}},
            )
            if required
            else ()
        )
        self.evaluation_requirements["repetition"] = ()
        self.calls = []

    def score_task(self, task_context, raw_evaluations, submission_facts):
        self.calls.append("task")
        return {"score": len(raw_evaluations)}

    def score_paper(self, paper_context, task_scores):
        self.calls.append("paper")
        return {"total": sum(x["score"]["score"] for x in task_scores)}


def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0\0" * 100)
    return out.getvalue()


@pytest.fixture
def qa_flow(db, bank, client, tmp_path):
    def make(required=False, mixed=False, with_image=False):
        plugin = QaPlugin(required)
        contracts = (
            [
                EvaluationContract(name, "1", frozenset({"qa"}), lambda p: p == {}, False)
                for name in ("test-text", "test-acoustic")
            ]
            if required
            else []
        )
        registry = Registry([plugin], contracts)
        images = {}
        image_data = None
        if with_image:
            from test_taskbank_import import picture

            from app.taskbank.models import Asset

            image_data = picture("red")
            image_ref, image_sha = AudioStore(tmp_path).put(image_data)
            asset_id = uuid4()
            with Session(db) as s, s.begin():
                s.add(
                    Asset(
                        asset_id=asset_id,
                        sha256=image_sha,
                        storage_ref=image_ref,
                        media_type="image/png",
                    )
                )
            images = {"question_image": asset_id}
        tasks = [
            write(
                db,
                bank,
                replace(
                    CONTENT,
                    task_type=kind,
                    guide_text=guide,
                    reference_answer="private reference",
                    scoring_instruction="private scoring",
                    ai_followup_scope="private followup",
                ),
                code=str(i),
                images=images if i == 0 else None,
            )
            for i, (kind, guide) in enumerate(
                (("qa", None), ("repetition" if mixed else "qa", "guide"))
            )
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
        store = RecordingStore(tmp_path / "recordings")
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
        return {
            "sid": sid,
            "paper": paper,
            "root": root,
            "headers": headers,
            "tasks": tasks,
            "store": store,
            "registry": registry,
            "plugin": plugin,
            "image_data": image_data,
        }

    return make


def base(f, index=0):
    return f"{f['root']}/tasks/{f['tasks'][index]['session_task_id']}"


def deliver(client, f, index=0, replay=0):
    url, h = base(f, index), f["headers"]
    content = client.get(url + "/content", headers=h)
    assert (
        content.status_code == 200
        and "private reference" not in content.text
        and "private scoring" not in content.text
        and "private followup" not in content.text
    )
    attempt = client.post(url + "/attempt", headers=h).json()["attempt_id"]
    assert client.get(url + "/attempt", headers=h).json()["attempt"]["attempt_id"] == attempt
    kinds = (
        (["GUIDE"] if content.json()["has_guide"] else [])
        + ["INITIAL_PROMPT"]
        + ["USER_REPLAY"] * replay
    )
    for kind in kinds:
        row = client.post(
            url + "/playbacks",
            headers=h,
            json={"attempt_id": attempt, "request_id": str(uuid4()), "kind": kind},
        )
        assert row.status_code == 200, row.text
        assert (
            client.post(
                url + "/playbacks/" + row.json()["playback_id"] + "/finish",
                headers=h,
                json={"attempt_id": attempt, "completed": True},
            ).status_code
            == 200
        )
    return attempt


def record(client, f, attempt, index=0, req="record"):
    return client.post(
        base(f, index) + "/recordings",
        headers=f["headers"],
        json={"attempt_id": attempt, "request_id": req},
    )


def upload(client, f, attempt, rid, data=None, index=0):
    return client.post(
        base(f, index) + "/recordings/" + rid + "/content",
        headers={**f["headers"], "X-Attempt-Id": attempt, "Content-Type": "audio/wav"},
        content=wav() if data is None else data,
    )


def submit(client, f, attempt, rid, index=0, request="submit", task_type="qa"):
    return client.post(
        base(f, index) + "/submissions/" + task_type,
        headers=f["headers"],
        json={"attempt_id": attempt, "recording_id": rid, "submit_request_id": request},
    )


def test_qa_http_recording_flow_and_recovery(db, client, qa_flow):
    f = qa_flow()
    h, url = f["headers"], base(f)
    before = {model: None for model in (Attempt, Recording, Playback, Submission)}
    with Session(db) as s:
        before = {m: s.scalar(select(func.count()).select_from(m)) for m in before}
    for _ in range(2):
        assert client.get(f["root"], headers=h).status_code == 200
        assert client.get(url + "/content", headers=h).status_code == 200
        assert client.get(url + "/recordings", headers=h).json() == []
    with Session(db) as s:
        assert {m: s.scalar(select(func.count()).select_from(m)) for m in before} == before
    aid = deliver(client, f, replay=2)
    row = record(client, f, aid)
    assert row.status_code == 200 and record(client, f, aid).json() == row.json()
    rid = row.json()["recording_id"]
    state = client.get(f["root"], headers=h).json()["tasks"][0]
    assert state["recording_in_progress"] and not state["playback_in_progress"]
    assert "UPLOAD_RECORDING" in state["allowed_actions"]
    assert submit(client, f, aid, rid).status_code == 409
    assert (
        client.post(
            url + "/playbacks",
            headers=h,
            json={"attempt_id": aid, "request_id": "busy", "kind": "USER_REPLAY"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            url + "/recordings/" + rid + "/cancel", headers=h, json={"attempt_id": aid}
        ).status_code
        == 200
    )
    rid = record(client, f, aid, req="new").json()["recording_id"]
    saved = upload(client, f, aid, rid)
    assert saved.status_code == 200 and upload(client, f, aid, rid).json() == saved.json()
    assert upload(client, f, aid, rid, b"different").status_code == 409
    assert client.get(saved.json()["content_url"], headers=h).content == wav()
    state = client.get(f["root"], headers=h).json()["tasks"][0]
    assert state["saved_recording_ids"] == [rid] and "SUBMIT_QA" in state["allowed_actions"]
    assert submit(client, f, aid, rid, task_type="repetition").status_code == 409
    result = submit(client, f, aid, rid)
    assert result.status_code == 200 and submit(client, f, aid, rid).json() == result.json()
    assert submit(client, f, aid, str(uuid4())).status_code == 409
    assert submit(client, f, str(uuid4()), rid).status_code == 409
    assert (
        client.post(
            url + "/submissions/qa",
            headers=h,
            json={
                "attempt_id": aid,
                "recording_id": rid,
                "submit_request_id": "submit",
                "asr_text": "untrusted",
            },
        ).status_code
        == 422
    )
    assert submit(client, f, aid, rid, task_type="repetition").status_code == 409
    with Session(db) as s:
        row = s.get(Submission, UUID(result.json()["submission_id"]))
        assert row.inputs["recording_id"] == rid and len(row.inputs["replay_playback_ids"]) == 2
    assert deliver(client, f, index=1)


def test_guide_technical_retry_and_frozen_revision(db, bank, client, qa_flow):
    f = qa_flow()
    old = client.get(base(f) + "/content", headers=f["headers"])
    write(
        db,
        bank,
        replace(CONTENT, task_type="qa", prompt_text="new content", reference_answer="new secret"),
        code="0",
    )
    assert (
        client.get(base(f) + "/content", headers=f["headers"]).json()["prompt_text"]
        == old.json()["prompt_text"]
    )
    url, h = base(f, 1), f["headers"]
    aid = client.post(url + "/attempt", headers=h).json()["attempt_id"]
    assert (
        client.post(
            url + "/playbacks",
            headers=h,
            json={"attempt_id": aid, "request_id": "early", "kind": "INITIAL_PROMPT"},
        ).status_code
        == 409
    )
    guide = client.post(
        url + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "guide", "kind": "GUIDE"},
    ).json()
    state = client.get(f["root"], headers=h).json()["tasks"][1]
    assert state["playback_in_progress"] and "FINISH_PLAYBACK" in state["allowed_actions"]
    assert record(client, f, aid, index=1).status_code == 409
    assert (
        client.post(
            url + "/playbacks/" + guide["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    prompt = client.post(
        url + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    ).json()
    assert (
        client.post(
            url + "/playbacks/" + prompt["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    retry = client.post(
        url + "/playbacks",
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
            url + "/playbacks/" + retry["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = record(client, f, aid, index=1).json()["recording_id"]
    assert upload(client, f, aid, rid, index=1).status_code == 200
    result = submit(client, f, aid, rid, index=1)
    assert result.status_code == 200
    with Session(db) as s:
        assert (
            s.get(Submission, UUID(result.json()["submission_id"])).inputs["replay_playback_ids"]
            == []
        )


def test_qa_failure_redo_and_cross_attempt(db, client, qa_flow):
    from sqlalchemy import event

    from app.sessions.close_manifest import CloseManifestEntry

    f = qa_flow()
    aid = deliver(client, f, replay=1)
    rid = record(client, f, aid).json()["recording_id"]
    assert upload(client, f, aid, rid).status_code == 200

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SUBMISSIONS"):
            raise RuntimeError("injected transaction failure")

    event.listen(db, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            submit(client, f, aid, rid)
    finally:
        event.remove(db, "before_cursor_execute", fail)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "SAVED"
        assert s.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None
    old = submit(client, f, aid, rid).json()["submission_id"]
    new = client.post(
        base(f) + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
    ).json()["attempt_id"]
    assert submit(client, f, new, rid, request="new").status_code == 409
    assert upload(client, f, aid, rid).status_code == 409
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 409
    )
    play = client.post(
        base(f) + "/playbacks",
        headers=f["headers"],
        json={"attempt_id": new, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    ).json()
    assert (
        client.post(
            base(f) + "/playbacks/" + play["playback_id"] + "/finish",
            headers=f["headers"],
            json={"attempt_id": new, "completed": True},
        ).status_code
        == 200
    )
    assert submit(client, f, new, rid, request="new").status_code == 409
    second = record(client, f, new).json()["recording_id"]
    assert upload(client, f, new, second).status_code == 200
    final = submit(client, f, new, second, request="new").json()["submission_id"]
    aid2 = deliver(client, f, index=1)
    rid2 = record(client, f, aid2, index=1).json()["recording_id"]
    assert upload(client, f, aid2, rid2, index=1).status_code == 200
    assert submit(client, f, aid2, rid2, index=1).status_code == 200
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    assert submit(client, f, new, second, request="new").status_code == 409
    with Session(db) as s:
        selected = {str(x) for x in s.scalars(select(CloseManifestEntry.submission_id))}
        assert old not in selected and final in selected
        assert s.get(Submission, UUID(final)).inputs["replay_playback_ids"] == []


def test_qa_corrupt_missing_and_other_session(db, client, qa_flow):
    f = qa_flow()
    aid = deliver(client, f)
    rid = record(client, f, aid).json()["recording_id"]
    saved = upload(client, f, aid, rid).json()
    other = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(f["paper"], "other")
    ).json()["session_id"]
    other_h = access(issue(client, other))
    assert client.get(saved["content_url"], headers=other_h).status_code == 401
    other_state = client.get(f"/assessment/sessions/{other}", headers=other_h).json()
    other_task = other_state["tasks"][0]["session_task_id"]
    assert (
        client.get(
            f"/assessment/sessions/{other}/tasks/{other_task}/recordings/{rid}/content",
            headers=other_h,
        ).status_code
        == 404
    )
    other_flow = {
        **f,
        "root": f"/assessment/sessions/{other}",
        "headers": other_h,
        "tasks": other_state["tasks"],
    }
    other_attempt = deliver(client, other_flow)
    assert submit(client, other_flow, other_attempt, rid).status_code == 409
    with Session(db) as s:
        row = s.get(Recording, UUID(rid))
        path = f["store"].path(row.storage_ref, row.sha256)
    path.write_bytes(b"corrupt")
    assert submit(client, f, aid, rid).status_code == 409
    path.unlink()
    assert submit(client, f, aid, rid).status_code == 409
    with Session(db) as s:
        assert s.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None


@pytest.mark.parametrize("failure", [False, True])
def test_qa_independent_evaluation_sources(db, client, qa_flow, failure):
    from datetime import timedelta

    from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure, run_once
    from app.scoring.models import FinalScoreResult
    from app.scoring.worker import run_settlement_once

    f = qa_flow(required=True)
    submission_ids = []
    for index in (0, 1):
        aid = deliver(client, f, index=index)
        rid = record(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        accepted = submit(client, f, aid, rid, index=index)
        assert accepted.status_code == 200
        assert submit(client, f, aid, rid, index=index).json() == accepted.json()
        submission_ids.append(accepted.json()["submission_id"])
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )

    class Adapter:
        def __init__(self, source):
            self.source = source

        def resolve_inputs(self, session, submission, revision, requirement):
            row = session.get(Recording, UUID(submission.inputs["recording_id"]))
            assert row.status == "SAVED" and row.attempt_id == submission.attempt_id
            assert revision.task_type == "qa"
            return InputSnapshot(
                refs={"recording_id": str(row.recording_id), "sha256": row.sha256},
                payload={"ref": row.storage_ref, "sha": row.sha256},
            )

        def evaluate(self, inputs, requirement):
            assert f["store"].read(inputs.payload["ref"], inputs.payload["sha"]) == wav()
            if failure and self.source == "test-acoustic":
                raise KnownFailure("TEST_FAILED")
            return AdapterResult(payload={"test_metric": 0, "source": self.source})

        def validate_result(self, result, requirement):
            return result.payload["source"] == self.source

    output = run_once(
        db,
        registry=f["registry"],
        adapters={(src, "1"): Adapter(src) for src in ("test-text", "test-acoustic")},
        lease=timedelta(minutes=1),
        batch_size=20,
    )
    assert output["finished"] == 4
    with Session(db) as s:
        jobs = list(s.scalars(select(EvaluationJob)))
        raws = list(s.scalars(select(RawEvaluation)))
        assert len(jobs) == len(raws) == 4
        assert {job.requirement["source"] for job in jobs} == {"test-text", "test-acoustic"}
        assert {str(job.submission_id) for job in jobs} == set(submission_ids)
        assert all(job.requirement["source"] != "iflytek_ise" for job in jobs)
        assert len({raw.evaluation_id for raw in raws}) == 4
        by_job = {job.job_id: job for job in jobs}
        for raw in raws:
            job = by_job[raw.job_id]
            submitted = s.get(Submission, job.submission_id)
            assert raw.submission_id == job.submission_id
            assert (
                raw.input_refs["dependencies"]["recording_id"] == submitted.inputs["recording_id"]
            )
        assert sorted(raw.status for raw in raws) == (
            ["FAILED", "FAILED", "READY", "READY"] if failure else ["READY"] * 4
        )
        assert s.scalar(select(func.count()).select_from(EvaluationIntent)) == 4
    result = run_settlement_once(db, registry=f["registry"])
    if failure:
        assert len(result["pending"]) == 1 and not result["completed"]
        with Session(db) as s:
            assert s.scalar(select(FinalScoreResult)) is None
    else:
        assert len(result["completed"]) == 1 and not result["failures"]
        assert client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH).json()["ready"]
        assert f["plugin"].calls == ["task", "task", "paper"]


def test_mixed_qa_and_repetition_freeze_distinct_submissions(db, client, qa_flow):
    from app.sessions.close_manifest import CloseManifestEntry

    f = qa_flow(mixed=True)
    chosen = []
    for index, kind in enumerate(("qa", "repetition")):
        aid = deliver(client, f, index=index)
        rid = record(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        wrong = "repetition" if kind == "qa" else "qa"
        assert submit(client, f, aid, rid, index=index, task_type=wrong).status_code == 409
        result = submit(client, f, aid, rid, index=index, task_type=kind)
        assert result.status_code == 200
        chosen.append(result.json()["submission_id"])
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    with Session(db) as s:
        assert {str(i) for i in s.scalars(select(CloseManifestEntry.submission_id))} == set(chosen)
        assert len(list(s.scalars(select(Recording).where(Recording.status == "SAVED")))) == 2


@pytest.mark.parametrize("kind", ["upload", "submission"])
def test_qa_token_revocation_blocks_inflight_write(db, client, qa_flow, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import app.api.assessment_repetition as api

    f = qa_flow()
    aid = deliver(client, f)
    rid = record(client, f, aid).json()["recording_id"]
    entered, release = Event(), Event()
    if kind == "upload":
        original = f["store"].commit

        def hold(path, digest):
            ref = original(path, digest)
            entered.set()
            assert release.wait(10)
            return ref

        monkeypatch.setattr(f["store"], "commit", hold)
        operation = lambda: upload(client, f, aid, rid)
    else:
        assert upload(client, f, aid, rid).status_code == 200
        original = api.confirm_write_access

        def hold(db_session, session_id, credential_id):
            entered.set()
            assert release.wait(10)
            return original(db_session, session_id, credential_id)

        monkeypatch.setattr(api, "confirm_write_access", hold)
        operation = lambda: submit(client, f, aid, rid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            assert entered.wait(10)
            assert (
                client.post(
                    f"/teacher/assessment/sessions/{f['sid']}/access-tokens/revoke-all",
                    headers=AUTH,
                ).status_code
                == 200
            )
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 401
    with Session(db) as s:
        row = s.get(Recording, UUID(rid))
        assert row.status == ("STARTED" if kind == "upload" else "SAVED")
        assert s.scalar(select(Submission).where(Submission.attempt_id == UUID(aid))) is None


@pytest.mark.parametrize(
    "operation,first",
    [("submit", "submit"), ("submit", "close"), ("redo", "redo"), ("redo", "close")],
)
def test_qa_close_competing_with_submit_or_redo(db, client, qa_flow, monkeypatch, operation, first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    import app.api.assessment_operations as common_api
    import app.api.assessment_repetition as recording_api
    from app.sessions.close_manifest import CloseManifest, CloseManifestEntry, IncompleteSession

    f = qa_flow()
    inputs = []
    for index in (0, 1):
        aid = deliver(client, f, index=index)
        rid = record(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        inputs.append((aid, rid))
    first_id = submit(client, f, *inputs[0]).json()["submission_id"]
    if operation == "redo":
        assert submit(client, f, *inputs[1], index=1).status_code == 200
    target = recording_api if first == "submit" else common_api
    name = {"submit": "submit_qa", "redo": "redo_attempt", "close": "close_session"}[first]
    original = getattr(target, name)
    entered, release, competing = Event(), Event(), Event()

    def hold(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except IncompleteSession:
            entered.set()
            assert release.wait(10)
            raise
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(target, name, hold)

    def call(action):
        if action == "submit":
            return submit(client, f, *inputs[1], index=1)
        if action == "redo":
            return client.post(
                base(f, 1) + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
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
            chosen = {
                str(x)
                for x in s.scalars(
                    select(CloseManifestEntry.submission_id).where(
                        CloseManifestEntry.manifest_id == manifest.manifest_id
                    )
                )
            }
            assert first_id in chosen and len(chosen) == 2
            if operation == "submit":
                assert responses["submit"].json()["submission_id"] in chosen
        else:
            assert manifest is None


def test_qa_redo_during_playback_rejects_late_receipt(db, client, qa_flow):
    f = qa_flow()
    aid = deliver(client, f)
    url, h = base(f), f["headers"]
    playback = client.post(
        url + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "replay", "kind": "USER_REPLAY"},
    ).json()
    redo = client.post(url + "/redo", headers=h, json={"redo_request_id": "redo"})
    assert redo.status_code == 200
    assert (
        client.post(
            url + "/playbacks/" + playback["playback_id"] + "/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 409
    )
    state = client.get(f["root"], headers=h).json()["tasks"][0]
    assert state["attempt_id"] == redo.json()["attempt_id"] and not state["playback_in_progress"]
    with Session(db) as s:
        assert s.get(Playback, UUID(playback["playback_id"])).status == "STARTED"


def test_qa_redo_rejects_late_upload(db, client, qa_flow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    f = qa_flow()
    aid = deliver(client, f)
    rid = record(client, f, aid).json()["recording_id"]
    entered, release = Event(), Event()
    original = f["store"].commit

    def hold(path, digest):
        ref = original(path, digest)
        entered.set()
        assert release.wait(10)
        return ref

    monkeypatch.setattr(f["store"], "commit", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload, client, f, aid, rid)
        try:
            assert entered.wait(10)
            assert (
                client.post(
                    base(f) + "/redo", headers=f["headers"], json={"redo_request_id": "redo"}
                ).status_code
                == 200
            )
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 409
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"


def test_qa_missing_upstream_never_fabricates_text_or_final_score(db, client, qa_flow):
    from datetime import timedelta

    from app.evaluation.worker import AdapterResult, InputSnapshot, run_once
    from app.scoring.models import FinalScoreResult
    from app.scoring.worker import run_settlement_once

    f = qa_flow(required=True)
    for index in (0, 1):
        aid = deliver(client, f, index=index)
        rid = record(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        assert submit(client, f, aid, rid, index=index).status_code == 200
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )

    class MissingText:
        def resolve_inputs(self, session, submission, revision, requirement):
            return None

        def evaluate(self, inputs, requirement):
            raise AssertionError("Missing upstream input must not be evaluated")

        def validate_result(self, result, requirement):
            return True

    class Acoustic:
        def resolve_inputs(self, session, submission, revision, requirement):
            row = session.get(Recording, UUID(submission.inputs["recording_id"]))
            return InputSnapshot(
                refs={"recording_id": str(row.recording_id)},
                payload={"ref": row.storage_ref, "sha": row.sha256},
            )

        def evaluate(self, inputs, requirement):
            assert f["store"].read(inputs.payload["ref"], inputs.payload["sha"]) == wav()
            return AdapterResult(payload={"test_metric": 0})

        def validate_result(self, result, requirement):
            return True

    out = run_once(
        db,
        registry=f["registry"],
        adapters={("test-text", "1"): MissingText(), ("test-acoustic", "1"): Acoustic()},
        lease=timedelta(minutes=1),
        batch_size=20,
    )
    assert out["finished"] == 2
    with Session(db) as s:
        jobs = list(s.scalars(select(EvaluationJob)))
        assert [job.status for job in jobs if job.requirement["source"] == "test-text"] == [
            "WAITING",
            "WAITING",
        ]
        assert len(list(s.scalars(select(RawEvaluation)))) == 2
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    with Session(db) as s:
        assert s.scalar(select(FinalScoreResult)) is None


def test_qa_missing_recording_cannot_produce_ready_evidence(db, client, qa_flow):
    from datetime import timedelta

    from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure, run_once
    from app.scoring.models import FinalScoreResult
    from app.scoring.worker import run_settlement_once

    f = qa_flow(required=True)
    for index in (0, 1):
        aid = deliver(client, f, index=index)
        rid = record(client, f, aid, index=index).json()["recording_id"]
        assert upload(client, f, aid, rid, index=index).status_code == 200
        assert submit(client, f, aid, rid, index=index).status_code == 200
    assert (
        client.post(
            f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    with Session(db) as s:
        row = s.get(Recording, UUID(rid))
        f["store"].path(row.storage_ref, row.sha256).unlink()

    class Adapter:
        def resolve_inputs(self, session, submission, revision, requirement):
            recording = session.get(Recording, UUID(submission.inputs["recording_id"]))
            return InputSnapshot(
                refs={"recording_id": str(recording.recording_id)},
                payload={"ref": recording.storage_ref, "sha": recording.sha256},
            )

        def evaluate(self, inputs, requirement):
            try:
                f["store"].verify(inputs.payload["ref"], inputs.payload["sha"])
            except ValueError as exc:
                raise KnownFailure("RECORDING_UNAVAILABLE") from exc
            return AdapterResult(payload={"test_metric": 0})

        def validate_result(self, result, requirement):
            return True

    adapters = {(src, "1"): Adapter() for src in ("test-text", "test-acoustic")}
    run_once(
        db, registry=f["registry"], adapters=adapters, lease=timedelta(minutes=1), batch_size=20
    )
    with Session(db) as s:
        raws = list(s.scalars(select(RawEvaluation)))
        assert len(raws) == 4 and all(
            row.status == "FAILED" and row.payload is None for row in raws
        )
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    with Session(db) as s:
        assert s.scalar(select(FinalScoreResult)) is None


def test_qa_upload_commit_failure_retries_same_recording(db, client, qa_flow):
    from sqlalchemy import event

    f = qa_flow()
    aid = deliver(client, f)
    rid = record(client, f, aid).json()["recording_id"]

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE RECORDINGS"):
            raise RuntimeError("injected upload failure")

    event.listen(db, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected upload failure"):
            upload(client, f, aid, rid)
    finally:
        event.remove(db, "before_cursor_execute", fail)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"
    assert upload(client, f, aid, rid).status_code == 200
    assert submit(client, f, aid, rid).status_code == 200


def test_qa_frozen_question_image_is_session_protected(db, bank, client, qa_flow):
    f = qa_flow(with_image=True)
    content = client.get(base(f) + "/content", headers=f["headers"]).json()
    assert content["question_image_url"]
    assert (
        client.get(content["question_image_url"], headers=f["headers"]).content == f["image_data"]
    )
    assert client.get(content["question_image_url"], headers=AUTH).status_code == 401
    write(db, bank, replace(CONTENT, task_type="qa", prompt_text="updated revision"), code="0")
    again = client.get(base(f) + "/content", headers=f["headers"]).json()
    assert again["task_revision_id"] == content["task_revision_id"]
    assert client.get(again["question_image_url"], headers=f["headers"]).content == f["image_data"]
