import importlib.util
import io
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Annotated
from uuid import UUID

import pytest
from auth_helpers import prepare_admin
from cryptography.fernet import Fernet
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.db import get_auth_db, get_db
from app.api.teacher_auth import require_admin
from app.main import app as production_app
from app.papers.paper import PaperEntry
from app.sessions.assessment_session import SessionTask
from app.taskbank.models import RevisionContent, TaskIdentity, TaskRevision, append_revision

spec = importlib.util.spec_from_file_location(
    "qa_workbench", Path(__file__).parent / "manual" / "workbench.py"
)
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


def prepare_login(db, monkeypatch):
    monkeypatch.setenv("VNEXT_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("VNEXT_AUTH_ALLOWED_ORIGINS", "http://127.0.0.1:8000")
    prepare_admin(db)


def login_admin(client):
    response = client.post(
        "/auth/login", json={"username": "test-admin", "password": "vnext-invalid-login-dummy"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


@pytest.mark.db_isolated
def test_workbench_separates_auth_and_business_sessions(db, tmp_path, monkeypatch):
    prepare_login(db, monkeypatch)
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = workbench.create_app(db, tmp_path)

    @app.get("/test-session-dependencies", dependencies=[Depends(require_admin)])
    def inspect_dependencies(
        business: Annotated[Session, Depends(get_db)], auth: Annotated[Session, Depends(get_auth_db)]
    ):
        return {"distinct": business is not auth, "same_engine": business.bind is auth.bind is db}

    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        assert client.get("/test-session-dependencies").status_code == 401
        login_admin(client)
        assert client.get("/test-session-dependencies").json() == {
            "distinct": True,
            "same_engine": True,
        }
        assert client.get("/teacher/llm-provider").status_code == 200
        response = client.put(
            "/teacher/llm-provider",
            json={"provider": "deepseek", "model": "deepseek-flash", "api_key": "test-key"},
        )
        assert response.status_code == 200, response.text
        assert client.get("/teacher/llm-provider").json()["provider"] == "deepseek"


@pytest.mark.db_isolated
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"task_type": "repetition"}, ("请跟读这句话。", "今天天气真好。", "adult", None)),
        (
            {"task_type": "repetition", "prompt_text": "冻结题干", "reference_answer": "冻结答案",
             "group": "pupil", "grade": "junior"},
            ("冻结题干", "冻结答案", "pupil", "junior"),
        ),
    ],
)
def test_workbench_displays_frozen_repetition(db, tmp_path, monkeypatch, body, expected):
    prepare_login(db, monkeypatch)
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        sid = client.post("/dev/qa/sessions", json=body).json()["session_id"]
        token = client.post(f"/dev/qa/sessions/{sid}/token").json()["token"]
        auth = {"Authorization": "Bearer " + token}
        root = f"/assessment/sessions/{sid}"
        tid = client.get(root, headers=auth).json()["tasks"][0]["session_task_id"]
        with Session(db) as session, session.begin():
            task = session.get(SessionTask, UUID(tid))
            entry = session.get(PaperEntry, task.paper_entry_id)
            revision = session.get(TaskRevision, entry.task_revision_id)
            identity = session.get(TaskIdentity, revision.task_id)
            append_revision(
                session,
                bank_id=identity.bank_id,
                task_code=identity.task_code,
                display_order=identity.display_order,
                content=RevisionContent(
                    grade_level="测试", task_type="repetition", prompt_text="新修订题干",
                    reference_answer="新修订答案",
                ),
                images={},
            )
        diagnostic = client.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={tid}")
        assert diagnostic.status_code == 200
        prompt, reference, group, grade = expected
        assert diagnostic.json()["frozen_task"] == {
            "task_type": "repetition", "prompt_text": prompt, "reference_answer": reference,
            "ise_group": group, "ise_grade": grade,
        }
        content = client.get(f"{root}/tasks/{tid}/content", headers=auth).json()
        assert content["prompt_text"] == prompt
        assert "reference_answer" not in content and "scoring_instruction" not in content
        client.cookies.clear()
        assert client.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={tid}", headers=auth).status_code == 401


@pytest.mark.parametrize("media_type", ["audio/wav", "audio/webm"])
@pytest.mark.db_isolated
def test_workbench_chain(db, tmp_path, media_type, monkeypatch):
    from app.evaluation.dashscope_asr import DashScopeASR
    from app.evaluation.qwen_qa_text import QwenQAText

    def forbidden(*args, **kwargs):
        raise AssertionError("Paid adapter must not run")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-placeholder")
    monkeypatch.setattr(DashScopeASR, "evaluate", forbidden)
    monkeypatch.setattr(QwenQAText, "evaluate", forbidden)
    if media_type == "audio/webm" and not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required for WebM validation")
    prepare_login(db, monkeypatch)
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        assert c.get("/dev/qa").status_code == 200
        assert c.get("/dev/qa/sessions").status_code == 401
        login_admin(c)
        assert c.post("/dev/qa/sessions", headers={"Origin": "http://evil.test"}).status_code == 403
        assert c.post("/dev/qa/run-real").status_code == 404
        created = c.post("/dev/qa/sessions")
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        token = c.post(f"/dev/qa/sessions/{sid}/token").json()["token"]
        auth = {"Authorization": "Bearer " + token}
        root = f"/assessment/sessions/{sid}"
        task = c.get(root, headers=auth).json()["tasks"][0]
        base = root + "/tasks/" + task["session_task_id"]
        content = c.get(base + "/content", headers=auth).json()
        assert content["prompt_text"] == "小猫在做什么？"
        assert "reference_answer" not in content and "scoring_instruction" not in content
        aid = c.post(base + "/attempt", headers=auth).json()["attempt_id"]
        play = c.post(
            base + "/playbacks",
            headers=auth,
            json={"attempt_id": aid, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
        ).json()
        assert c.get(play["audio_url"], headers=auth).status_code == 200
        assert (
            c.post(
                base + "/playbacks/" + play["playback_id"] + "/finish",
                headers=auth,
                json={"attempt_id": aid, "completed": True},
            ).status_code
            == 200
        )
        replay = c.post(
            base + "/playbacks",
            headers=auth,
            json={"attempt_id": aid, "request_id": "replay", "kind": "USER_REPLAY"},
        ).json()
        assert (
            c.post(
                base + "/playbacks/" + replay["playback_id"] + "/finish",
                headers=auth,
                json={"attempt_id": aid, "completed": False},
            ).status_code
            == 200
        )
        retry = c.post(
            base + "/playbacks",
            headers=auth,
            json={
                "attempt_id": aid,
                "request_id": "retry",
                "kind": "TECHNICAL_RETRY",
                "retry_of_id": replay["playback_id"],
            },
        ).json()
        assert (
            c.post(
                base + "/playbacks/" + retry["playback_id"] + "/finish",
                headers=auth,
                json={"attempt_id": aid, "completed": True},
            ).status_code
            == 200
        )
        rid = c.post(
            base + "/recordings", headers=auth, json={"attempt_id": aid, "request_id": "record"}
        ).json()["recording_id"]
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 1600)
        audio_bytes = data.getvalue()
        if media_type == "audio/webm":
            audio_bytes = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    "pipe:0",
                    "-c:a",
                    "libopus",
                    "-f",
                    "webm",
                    "pipe:1",
                ],
                input=audio_bytes,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
        response = c.post(
            base + "/recordings/" + rid + "/content",
            headers={**auth, "X-Attempt-Id": aid, "Content-Type": media_type},
            content=audio_bytes,
        )
        assert response.status_code == 200, response.text
        body = {"attempt_id": aid, "recording_id": rid, "submit_request_id": "submit"}
        submit = c.post(base + "/submissions/qa", headers=auth, json=body)
        assert submit.status_code == 200, submit.text
        assert c.post(base + "/submissions/qa", headers=auth, json=body).json() == submit.json()
        result = c.post(f"/dev/qa/sessions/{sid}/run-offline")
        assert result.status_code == 200, result.text
        diag = c.get(f"/dev/qa/sessions/{sid}/diagnostics").json()
        assert [j["status"] for j in diag["jobs"]] == ["READY", "READY"]
        assert all(j["evidence"]["provider"] == "Mock" for j in diag["jobs"])
        assert diag["jobs"][0]["evidence"]["payload"]["original_audio"]["codec"] == (
            "opus" if media_type == "audio/webm" else "pcm_s16le"
        )
        assert (
            diag["jobs"][1]["input_refs"]["dependencies"]["asr"]["evaluation_id"]
            == diag["jobs"][0]["evidence"]["id"]
        )
        c.post(f"/dev/qa/sessions/{sid}/run-offline")
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics").json() == diag
        assert (
            c.post(
                base + "/redo",
                headers=auth,
                json={"redo_request_id": "redo", "expected_attempt_id": aid},
            ).status_code
            == 200
        )
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics").json()["submission"] is None
    assert not any(
        getattr(route, "path", "").startswith("/dev/qa") for route in production_app.routes
    )


@pytest.mark.db_isolated
def test_two_tasks_delayed_evaluation_and_redo_isolation(db, tmp_path, monkeypatch):
    prepare_login(db, monkeypatch)
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        login_admin(c)
        sid = c.post("/dev/qa/sessions?count=2").json()["session_id"]
        token = c.post(f"/dev/qa/sessions/{sid}/token").json()["token"]
        auth = {"Authorization": "Bearer " + token}
        root = f"/assessment/sessions/{sid}"
        tasks = c.get(root, headers=auth).json()["tasks"]
        assert len(tasks) == 2
        ids = [t["session_task_id"] for t in tasks]
        assert len(set(ids)) == 2
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 1600)

        def start(index):
            base = f"{root}/tasks/{ids[index]}"
            aid = c.post(base + "/attempt", headers=auth).json()["attempt_id"]
            play = c.post(
                base + "/playbacks",
                headers=auth,
                json={"attempt_id": aid, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
            ).json()
            assert (
                c.post(
                    base + "/playbacks/" + play["playback_id"] + "/finish",
                    headers=auth,
                    json={"attempt_id": aid, "completed": True},
                ).status_code
                == 200
            )
            return base, aid

        def submit(base, aid):
            rid = c.post(
                base + "/recordings", headers=auth, json={"attempt_id": aid, "request_id": "record"}
            ).json()["recording_id"]
            response = c.post(
                base + "/recordings/" + rid + "/content",
                content=data.getvalue(),
                headers={**auth, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
            )
            assert response.status_code == 200
            body = {"attempt_id": aid, "recording_id": rid, "submit_request_id": f"submit-{aid}"}
            first = c.post(base + "/submissions/qa", headers=auth, json=body).json()
            assert c.post(base + "/submissions/qa", headers=auth, json=body).json() == first
            return rid, first["submission_id"]

        first_base, a1 = start(0)
        r1, s1 = submit(first_base, a1)
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[0]}").json()["jobs"] == []
        second_base, a2 = start(1)
        assert a1 != a2
        assert c.post(f"/dev/qa/sessions/{sid}/run-offline?task_id={ids[0]}").status_code == 200
        first = c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[0]}").json()
        assert first["submission"]["submission_id"] == s1
        assert all(
            j["status"] == "READY" and j["input_refs"]["submission_id"] == s1 for j in first["jobs"]
        )
        r2, s2 = submit(second_base, a2)
        assert r1 != r2 and s1 != s2
        assert c.post(f"/dev/qa/sessions/{sid}/run-offline?task_id={ids[1]}").status_code == 200
        second = c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[1]}").json()
        assert second["recording"]["recording_id"] == r2
        assert all(j["input_refs"]["submission_id"] == s2 for j in second["jobs"])
        assert first["jobs"][0]["evidence"]["id"] != second["jobs"][0]["evidence"]["id"]
        state = c.get(root, headers=auth).json()
        assert all(t["submission_exists"] for t in state["tasks"])
        assert (
            c.post(
                second_base + "/redo",
                headers=auth,
                json={"redo_request_id": "redo", "expected_attempt_id": a2},
            ).status_code
            == 200
        )
        assert (
            c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[1]}").json()["submission"]
            is None
        )
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[0]}").json() == first
        second_base, new_attempt = start(1)
        assert new_attempt != a2
        new_recording, new_submission = submit(second_base, new_attempt)
        assert new_recording != r2 and new_submission != s2
        assert c.post(f"/dev/qa/sessions/{sid}/run-offline?task_id={ids[1]}").status_code == 200
        replacement = c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[1]}").json()
        assert replacement["submission"]["submission_id"] == new_submission
        assert all(j["input_refs"]["submission_id"] == new_submission for j in replacement["jobs"])
        assert replacement["jobs"][0]["evidence"]["id"] != second["jobs"][0]["evidence"]["id"]
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics?task_id={ids[0]}").json() == first
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics").status_code == 404


def test_workbench_rejects_non_test_database_before_connecting(tmp_path):
    from sqlalchemy import create_engine, event

    engine = create_engine("postgresql+psycopg://fixture@127.0.0.1:5432/production")
    calls = []
    event.listen(engine, "connect", lambda *args: calls.append("connected"))
    try:
        with pytest.raises(ValueError, match="local test database"):
            workbench.create_app(engine, tmp_path)
        assert calls == []
    finally:
        engine.dispose()
