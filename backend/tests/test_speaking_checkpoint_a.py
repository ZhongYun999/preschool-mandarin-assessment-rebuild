"""Speaking initial delivery and persistent checkpoint A."""

import subprocess
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_assessment_http import access, create_body, issue
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG, TestPlugin
from test_qa_http import wav
from test_taskbank import CONTENT, write
from test_teacher_api import AUTH

from app.api import assessment_repetition
from app.api.assessment_repetition import get_recording_store
from app.api.teacher_api import get_audio_store
from app.api.teacher_review import get_scoring_registry
from app.main import app
from app.media.audio_inspection import AudioError, AudioTools
from app.media.fixed_audio import AudioStore, prepare_audio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.registry import Registry
from app.task_types.speaking import SpeakingSegment

pytest_plugins = ("test_teacher_api",)


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code"),
    [
        ("ffprobe_missing", 503, "AUDIO_TOOL_UNAVAILABLE"),
        ("ffmpeg_missing", 503, "AUDIO_TOOL_UNAVAILABLE"),
        ("timeout", 504, "AUDIO_TOOL_TIMEOUT"),
        ("tool_failed", 500, "AUDIO_TOOL_FAILED"),
        ("resource_limit", 413, "AUDIO_RESOURCE_LIMIT"),
        ("decoded_limit", 413, "AUDIO_RESOURCE_LIMIT"),
        ("invalid_audio", 422, "AUDIO_UNRECOGNIZED"),
        ("empty_audio", 422, "AUDIO_UNRECOGNIZED"),
        ("damaged_audio", 422, "AUDIO_DECODE_FAILED"),
    ],
)
def test_audio_errors_preserve_recording_and_allow_retry(
    db, client, flow, monkeypatch, failure, expected_status, expected_code
):
    root, base, headers, aid = flow(None)
    pid = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=headers, json={"attempt_id": aid, "request_id": "rec"}
    ).json()["recording_id"]
    url = base + f"/recordings/{rid}/content"
    upload_headers = {**headers, "X-Attempt-Id": aid, "Content-Type": "audio/wav"}
    with monkeypatch.context() as patch:
        if failure == "ffprobe_missing":
            patch.setattr(
                assessment_repetition,
                "AudioTools",
                lambda: AudioTools(ffprobe="nonexistent-ffprobe-for-test"),
            )
        elif failure == "ffmpeg_missing":
            patch.setattr(
                assessment_repetition,
                "AudioTools",
                lambda: AudioTools(ffmpeg="nonexistent-ffmpeg-for-test"),
            )
        elif failure == "timeout":

            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired("test-audio-tool", 1)

            patch.setattr(subprocess, "run", timeout)
        elif failure in ("tool_failed", "decoded_limit"):

            def tool_failed(*args, **kwargs):
                raise AudioError(
                    "AUDIO_TOOL_FAILED" if failure == "tool_failed" else "AUDIO_RESOURCE_LIMIT"
                )

            patch.setattr(assessment_repetition, "inspect_audio", tool_failed)
        elif failure == "resource_limit":
            patch.setattr(
                assessment_repetition, "AudioTools", lambda: AudioTools(max_input_bytes=10)
            )
        content = b"invalid audio" if failure == "invalid_audio" else wav()
        if failure == "empty_audio":
            content = b""
        if failure == "damaged_audio":
            content = content[:-50]
        response = client.post(url, headers=upload_headers, content=content)
    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"
        assert s.scalar(select(SpeakingSegment)) is None
    assert not client.get(root, headers=headers).json()["tasks"][0]["speaking"]["checkpoint_a"]
    assert client.post(url, headers=upload_headers, content=wav()).status_code == 200
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "SAVED"
        assert len(s.scalars(select(SpeakingSegment)).all()) == 1


@pytest.fixture
def flow(db, bank, client, tmp_path):
    def create(guide):
        plugin = TestPlugin()
        plugin.evaluation_requirements["speaking"] = ()
        registry = Registry([plugin])
        task = write(db, bank, replace(CONTENT, task_type="speaking", guide_text=guide))
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
        sid = client.post(
            "/teacher/assessment/sessions", headers=AUTH, json=create_body(paper)
        ).json()["session_id"]
        h = access(issue(client, sid))
        root = f"/assessment/sessions/{sid}"
        task_id = client.get(root, headers=h).json()["tasks"][0]["session_task_id"]
        base = f"{root}/tasks/{task_id}"
        aid = client.post(base + "/attempt", headers=h).json()["attempt_id"]
        return root, base, h, aid

    yield create
    app.dependency_overrides.pop(get_audio_store, None)
    app.dependency_overrides.pop(get_recording_store, None)
    app.dependency_overrides.pop(get_scoring_registry, None)


@pytest.mark.parametrize("guide", [None, "guide"])
def test_checkpoint_and_recovery(db, client, flow, guide):
    root, base, h, aid = flow(guide)

    def state():
        return client.get(root, headers=h).json()["tasks"][0]

    def start_play(kind, request, **extra):
        return client.post(
            base + "/playbacks",
            headers=h,
            json={"attempt_id": aid, "request_id": request, "kind": kind, **extra},
        )

    def finish(pid, success):
        return client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": success},
        )

    def begin(req):
        return client.post(
            base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": req}
        )

    assert begin("too-early").status_code == 409
    assert "START_RECORDING" not in state()["allowed_actions"]
    if guide:
        assert start_play("INITIAL_PROMPT", "early").status_code == 409
        g = start_play("GUIDE", "g").json()["playback_id"]
        assert finish(g, True).status_code == 200
        assert finish(g, True).status_code == 200
        assert begin("too-early").status_code == 409
    else:
        assert start_play("GUIDE", "g").status_code == 409
    p = start_play("INITIAL_PROMPT", "p").json()["playback_id"]
    assert start_play("INITIAL_PROMPT", "other").status_code == 409
    assert begin("too-early").status_code == 409
    assert finish(p, False).status_code == 200
    retry = start_play("TECHNICAL_RETRY", "retry", retry_of_id=p).json()["playback_id"]
    assert finish(retry, True).status_code == 200
    for _ in range(5):  # A client may retry a failed HTTP request up to five times.
        assert finish(retry, True).status_code == 200
    conflict = finish(retry, False)
    assert conflict.status_code == 409 and "COMPLETED" in conflict.json()["detail"]
    assert "START_RECORDING" in state()["allowed_actions"]
    rid = begin("rec").json()["recording_id"]
    headers = {**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"}
    url = base + f"/recordings/{rid}/content"
    assert client.post(url, headers=headers, content=b"bad").status_code == 422
    assert client.post(url, headers=headers, content=wav()).status_code == 200
    assert client.post(url, headers=headers, content=wav()).status_code == 200
    assert client.post(url, headers=headers, content=wav() + b"x").status_code == 409
    current = state()
    assert current["speaking"]["phase"] == "ANALYZING_AUDIO"
    assert current["speaking"]["asr_status"] == "WAITING"
    assert current["speaking"]["total_duration_seconds"] == pytest.approx(100 / 16000)
    assert begin("next").status_code == 409
    with Session(db) as s:
        assert len(s.scalars(select(SpeakingSegment)).all()) == 1
    with (
        pytest.raises(DBAPIError, match="speaking segment facts must be preserved"),
        db.begin() as conn,
    ):
        conn.execute(text("UPDATE speaking_segments SET position=position+1"))
    redo = client.post(
        base + "/redo", headers=h, json={"redo_request_id": "redo", "expected_attempt_id": aid}
    ).json()
    assert redo["current_attempt_id"] != aid
    assert not state()["speaking"]["segments"]
    assert client.post(url, headers=headers, content=wav()).status_code == 409
    with Session(db) as s:
        assert s.scalar(select(SpeakingSegment)).attempt_id == UUID(aid)


def test_webm_decoded_duration_and_open_playback_redo(db, client, flow, tmp_path):
    root, base, h, aid = flow(None)
    first = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    redo = client.post(
        base + "/redo", headers=h, json={"redo_request_id": "redo", "expected_attempt_id": aid}
    ).json()
    new = redo["attempt_id"]
    assert (
        client.post(
            base + f"/playbacks/{first}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 409
    )
    state = client.get(root, headers=h).json()["tasks"][0]
    assert not state["initial_prompt_complete"]
    prompt = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": new, "request_id": "p2", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{prompt}/finish",
            headers=h,
            json={"attempt_id": new, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": new, "request_id": "rec"}
    ).json()["recording_id"]
    audio = tmp_path / "sample.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-c:a",
            "libopus",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    response = client.post(
        base + f"/recordings/{rid}/content",
        headers={**h, "X-Attempt-Id": new, "Content-Type": "audio/webm"},
        content=audio.read_bytes(),
    )
    assert response.status_code == 200, response.text
    state = client.get(root, headers=h).json()["tasks"][0]
    assert state["speaking"]["total_duration_seconds"] == pytest.approx(0.25, abs=0.02)


def test_redo_while_upload_waits_does_not_save_old_segment(db, client, flow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root, base, h, aid = flow(None)
    pid = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "rec"}
    ).json()["recording_id"]
    store = app.dependency_overrides[get_recording_store]()
    original = store.commit
    entered, release = Event(), Event()

    def held(path, digest):
        ref = original(path, digest)
        entered.set()
        assert release.wait(10)
        return ref

    monkeypatch.setattr(store, "commit", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post,
            base + f"/recordings/{rid}/content",
            headers={**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
            content=wav(),
        )
        try:
            assert entered.wait(10)
            assert (
                client.post(
                    base + "/redo",
                    headers=h,
                    json={"redo_request_id": "redo", "expected_attempt_id": aid},
                ).status_code
                == 200
            )
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 409
    with Session(db) as s:
        assert s.scalar(select(SpeakingSegment)) is None
    assert client.get(root, headers=h).json()["tasks"][0]["speaking"]["segments"] == []


def test_failed_segment_transaction_leaves_recording_unconfirmed(db, client, flow):
    root, base, h, aid = flow(None)
    pid = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "rec"}
    ).json()["recording_id"]
    url = base + f"/recordings/{rid}/content"
    headers = {**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"}

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SPEAKING_SEGMENTS"):
            raise RuntimeError("injected segment failure")

    event.listen(db, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected segment failure"):
            client.post(url, headers=headers, content=wav())
    finally:
        event.remove(db, "before_cursor_execute", fail)
    with Session(db) as s:
        assert s.get(Recording, UUID(rid)).status == "STARTED"
        assert s.scalar(select(SpeakingSegment)) is None
    assert not client.get(root, headers=h).json()["tasks"][0]["speaking"]["checkpoint_a"]
    assert client.post(url, headers=headers, content=wav()).status_code == 200


def test_concurrent_identical_uploads_save_one_segment(db, client, flow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, base, h, aid = flow(None)
    pid = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "rec"}
    ).json()["recording_id"]
    store = app.dependency_overrides[get_recording_store]()
    commit, barrier = store.commit, Barrier(2)

    def held(path, digest):
        result = commit(path, digest)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "commit", held)

    def upload(_):
        return client.post(
            base + f"/recordings/{rid}/content",
            headers={**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
            content=wav(),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(upload, range(2))) == [200, 200]
    with Session(db) as s:
        assert len(s.scalars(select(SpeakingSegment)).all()) == 1


def test_session_close_during_upload_stays_open(db, client, flow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    root, base, h, aid = flow(None)
    pid = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "r"}
    ).json()["recording_id"]
    store = app.dependency_overrides[get_recording_store]()
    commit, entered, release = store.commit, Event(), Event()

    def held(path, digest):
        result = commit(path, digest)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(store, "commit", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post,
            base + f"/recordings/{rid}/content",
            headers={**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
            content=wav(),
        )
        try:
            assert entered.wait(10)
            assert (
                client.post(
                    root + "/close", headers=h, json={"close_request_id": "close"}
                ).status_code
                == 409
            )
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200
    state = client.get(root, headers=h).json()
    assert state["status"] == "IN_PROGRESS" and state["tasks"][0]["speaking"]["checkpoint_a"]
