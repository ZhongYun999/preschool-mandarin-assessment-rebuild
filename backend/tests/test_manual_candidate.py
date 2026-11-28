"""Local workbench uses official QA providers and explicit paid-call authorization."""

import base64
import importlib.util
import io
import json
import wave
from pathlib import Path

import dashscope
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_dashscope_asr import body as asr_body
from test_iflytek_ise import FakeSocket
from test_manual_workbench import login_admin, prepare_login
from test_qa_formal_text_contract import RESULT

spec = importlib.util.spec_from_file_location(
    "candidate_workbench", Path(__file__).parent / "manual" / "workbench.py"
)
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


def submit_task(client, mode, task_type):
    created = client.post(
        f"/dev/qa/sessions?mode={mode}",
        json={
            "task_type": task_type,
            "prompt_text": "请跟读测试。",
            "reference_answer": "今天天气很好。",
        },
    )
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    token = client.post(f"/dev/qa/sessions/{sid}/token").json()["token"]
    auth = {"Authorization": "Bearer " + token}
    root = f"/assessment/sessions/{sid}"
    tid = client.get(root, headers=auth).json()["tasks"][0]["session_task_id"]
    base = f"{root}/tasks/{tid}"
    aid = client.post(base + "/attempt", headers=auth).json()["attempt_id"]
    playback = client.post(
        base + "/playbacks",
        headers=auth,
        json={"attempt_id": aid, "request_id": "play", "kind": "INITIAL_PROMPT"},
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + playback["playback_id"] + "/finish",
            headers=auth,
            json={"attempt_id": aid, "completed": True},
        ).status_code
        == 200
    )
    rid = client.post(
        base + "/recordings", headers=auth, json={"attempt_id": aid, "request_id": "record"}
    ).json()["recording_id"]
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 1600)
    assert (
        client.post(
            base + f"/recordings/{rid}/content",
            headers={**auth, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
            content=output.getvalue(),
        ).status_code
        == 200
    )
    result = client.post(
        base + f"/submissions/{task_type}",
        headers=auth,
        json={"attempt_id": aid, "recording_id": rid, "submit_request_id": "submit"},
    )
    assert result.status_code == 200, result.text
    return sid, tid, auth


def prepare_env(db, monkeypatch):
    prepare_login(db, monkeypatch)
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-asr-key")
    monkeypatch.setenv("VNEXT_QA_ASR_REGION", "cn-beijing")
    monkeypatch.setenv("VNEXT_QA_ASR_WORKSPACE", "asr-workspace")


def set_provider(client, provider):
    body = {
        "provider": provider,
        "model": "qwen-plus" if provider == "dashscope" else "deepseek-flash",
        "api_key": "fixture-text-key",
    }
    if provider == "dashscope":
        body.update(region="cn-beijing", workspace="text-workspace")
    result = client.put("/teacher/llm-provider", json=body)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.db_isolated
@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize("scenario", ["text", "no_text", "rejected", "timeout", "invalid"])
def test_formal_qa_chain_is_authorized_and_uses_current_provider(
    db, tmp_path, monkeypatch, provider, scenario
):
    prepare_env(db, monkeypatch)
    calls = []

    def handler(request):
        if "multimodal" in request.url.path:
            assert request.headers["authorization"] == "Bearer fixture-asr-key"
            calls.append("asr")
            if scenario == "rejected":
                return httpx.Response(401, json={"code": "InvalidApiKey"})
            if scenario == "timeout":
                raise httpx.ReadTimeout("simulated")
            return httpx.Response(200, json=asr_body("" if scenario == "no_text" else "测试文本"))
        assert request.headers["authorization"] == "Bearer fixture-text-key"
        calls.append(provider)
        content = (
            "invalid json" if scenario == "invalid" else json.dumps(RESULT, ensure_ascii=False)
        )
        choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        if provider == "dashscope":
            return httpx.Response(
                200,
                json={
                    "request_id": "test-id",
                    "model": "qwen-plus",
                    "output": {"choices": [choice]},
                    "usage": {},
                },
            )
        return httpx.Response(
            200, json={"id": "test-id", "model": "deepseek-flash", "choices": [choice], "usage": {}}
        )

    transport = httpx.MockTransport(handler)

    def dash_factory(**kwargs):
        return dashscope.Client(**kwargs, http_client=httpx.Client(transport=transport))

    def deep_factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    app = workbench.create_app(
        db,
        tmp_path,
        paid_enabled=True,
        client_factory=dash_factory,
        text_client_factories={"dashscope": dash_factory, "deepseek": deep_factory},
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        assert client.get("/dev/qa/sessions").status_code == 401
        login_admin(client)
        set_provider(client, "dashscope")
        sid, tid, _ = submit_task(client, "qwen", "qa")
        if provider == "deepseek":
            set_provider(client, provider)
        root = f"/dev/qa/sessions/{sid}"
        query = f"?task_id={tid}"
        assert client.get(root + "/diagnostics" + query).json()["jobs"] == [] and not calls
        assert client.post(root + "/run-candidate" + query).status_code == 403
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": False, "confirm_paid": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 200
        )
        run = client.post(root + "/run-candidate" + query)
        assert run.status_code == 200, run.text
        diagnostic = client.get(root + "/diagnostics" + query).json()
        expected = {
            "text": ["READY", "READY"],
            "no_text": ["READY", "WAITING"],
            "rejected": ["FAILED", "WAITING"],
            "timeout": ["UNKNOWN", "WAITING"],
            "invalid": ["READY", "INVALID"],
        }
        assert [job["status"] for job in diagnostic["jobs"]] == expected[scenario]
        assert calls == (["asr", provider] if scenario in ("text", "invalid") else ["asr"])
        assert diagnostic["requirements"][0]["parameters"]["workspace"] == "asr-workspace"
        assert "fixture-asr-key" not in str(diagnostic) and "fixture-text-key" not in str(
            diagnostic
        )
        if scenario in ("text", "invalid"):
            assert diagnostic["jobs"][1]["execution_snapshot"]["provider"] == provider
        client.get(root + "/diagnostics" + query)
        assert calls == (["asr", provider] if scenario in ("text", "invalid") else ["asr"])


@pytest.mark.db_isolated
def test_default_server_disables_paid_calls(db, tmp_path, monkeypatch):
    prepare_env(db, monkeypatch)

    def forbidden(**kwargs):
        raise AssertionError("No provider client may be created")

    app = workbench.create_app(db, tmp_path, client_factory=forbidden)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        set_provider(client, "dashscope")
        sid, tid, _ = submit_task(client, "qwen", "qa")
        root = f"/dev/qa/sessions/{sid}"
        query = f"?task_id={tid}"
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 403
        )
        assert client.post(root + "/run-candidate" + query).status_code == 403


@pytest.mark.db_isolated
def test_ise_workbench_explicit_authorization_and_rejection_history(db, tmp_path, monkeypatch):
    prepare_env(db, monkeypatch)
    for name in ("IFLYTEK_ISE_APP_ID", "IFLYTEK_ISE_API_KEY", "IFLYTEK_ISE_API_SECRET"):
        monkeypatch.setenv(name, "fake-" + name)
    xml = '<xml_result><read_sentence><rec_paper><read_sentence total_score="90" is_rejected="true" except_info="28676"/></rec_paper></read_sentence></xml_result>'
    calls = []

    def factory(*args, **kwargs):
        calls.append("ise")
        result = (
            xml
            if len(calls) == 1
            else xml.replace('is_rejected="true"', 'is_rejected="false"').replace(
                'except_info="28676"', 'except_info="0"'
            )
        )
        return FakeSocket(
            json.dumps(
                {
                    "code": 0,
                    "data": {"status": 2, "data": base64.b64encode(result.encode()).decode()},
                }
            )
        )

    app = workbench.create_app(db, tmp_path, paid_enabled=True, ise_socket_factory=factory)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        state = client.get("/dev/qa/ise-status").json()
        assert state["configured"] and state["last_call"] is None
        sid, tid, child = submit_task(client, "ise", "repetition")
        root = f"/dev/qa/sessions/{sid}"
        query = f"?task_id={tid}"
        assert client.get(root + "/diagnostics" + query).json()["jobs"] == [] and calls == []
        assert client.post(root + "/run-candidate" + query).status_code == 403
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 200
        )
        assert client.post(root + "/run-candidate" + query).status_code == 200
        diag = client.get(root + "/diagnostics" + query).json()
        assert diag["frozen_task"] == {
            "task_type": "repetition",
            "prompt_text": "请跟读测试。",
            "reference_answer": "今天天气很好。",
            "ise_group": "adult",
            "ise_grade": None,
        }
        assert diag["jobs"][0]["status"] == "FAILED" and diag["jobs"][0]["error"] == "ISE_REJECTED"
        assert diag["jobs"][0]["diagnostics"][0]["evidence"]["xml"] == xml
        assert calls == ["ise"]
        assert client.get("/dev/qa/ise-status").json()["last_call"]["status"] == "FAILED"
        assert "xml_result" not in client.get(f"/assessment/sessions/{sid}", headers=child).text
        job_id = diag["jobs"][0]["job_id"]
        assert (
            client.post(
                root + f"/jobs/{job_id}/retry" + query, json={"accept_unknown": False}
            ).status_code
            == 200
        )
        assert client.post(root + "/run-candidate" + query).status_code == 403
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 200
        )
        assert client.post(root + "/run-candidate" + query).status_code == 200
        retried = client.get(root + "/diagnostics" + query).json()["jobs"][0]
        assert (
            retried["status"] == "READY"
            and retried["evidence"]["payload"]["scores"]["total_score"] == 90
        )
        assert retried["diagnostics"][0]["evidence"]["xml"] == xml and calls == ["ise", "ise"]


@pytest.mark.db_isolated
def test_parallel_paid_requests_do_not_consume_llm_consent_early(db, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    prepare_env(db, monkeypatch)
    started, release = Event(), Event()
    calls = []

    def handler(request):
        if "multimodal" in request.url.path:
            calls.append("asr")
            started.set()
            assert release.wait(10)
            return httpx.Response(200, json=asr_body("测试文本"))
        calls.append("llm")
        choice = {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(RESULT, ensure_ascii=False)},
        }
        return httpx.Response(
            200,
            json={
                "request_id": "test-id",
                "model": "qwen-plus",
                "output": {"choices": [choice]},
                "usage": {},
            },
        )

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return dashscope.Client(**kwargs, http_client=httpx.Client(transport=transport))

    app = workbench.create_app(
        db,
        tmp_path,
        paid_enabled=True,
        client_factory=factory,
        text_client_factories={"dashscope": factory},
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        set_provider(client, "dashscope")
        sid, tid, _ = submit_task(client, "qwen", "qa")
        root, query = f"/dev/qa/sessions/{sid}", f"?task_id={tid}"
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 200
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, root + "/run-candidate" + query)
            try:
                assert started.wait(10)
                second = pool.submit(client.post, root + "/run-candidate" + query)
                assert second.result(timeout=10).status_code == 200
            finally:
                release.set()
            assert first.result(timeout=10).status_code == 200
        assert calls == ["asr", "llm"]
        diag = client.get(root + "/diagnostics" + query).json()
        assert [job["status"] for job in diag["jobs"]] == ["READY", "READY"]


@pytest.mark.db_isolated
def test_workbench_requires_admin_not_just_teacher(db, tmp_path, monkeypatch):
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from app.api.teacher_auth import dummy_hash
    from app.auth.models import User

    prepare_env(db, monkeypatch)
    with Session(db) as session, session.begin():
        session.add(
            User(
                user_id=uuid4(),
                username="teacher-local",
                display_name="Teacher",
                password_hash=dummy_hash,
                role="teacher",
                is_active=True,
            )
        )
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        assert client.get("/dev/qa/sessions").status_code == 401
        logged = client.post(
            "/auth/login",
            json={"username": "teacher-local", "password": "vnext-invalid-login-dummy"},
        )
        assert logged.status_code == 200
        client.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
        assert client.get("/dev/qa/sessions").status_code == 403
        assert client.post("/dev/qa/sessions", json={}).status_code == 403
        assert client.get("/teacher/llm-provider").status_code == 403


@pytest.mark.db_isolated
def test_repetition_mock_never_calls_ise(db, tmp_path, monkeypatch):
    prepare_env(db, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("Mock must not open ISE")

    app = workbench.create_app(db, tmp_path, paid_enabled=True, ise_socket_factory=forbidden)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        sid, tid, _ = submit_task(client, "mock", "repetition")
        root, query = f"/dev/qa/sessions/{sid}", f"?task_id={tid}"
        result = client.post(root + "/run-offline" + query)
        assert result.status_code == 200, result.text
        diagnostic = client.get(root + "/diagnostics" + query).json()
        assert diagnostic["mode"] == "mock-ise"
        assert diagnostic["jobs"][0]["status"] == "READY"
        assert diagnostic["jobs"][0]["evidence"]["payload"] == {"simulated": True}


@pytest.mark.db_isolated
def test_ise_unknown_requires_explicit_retry_acknowledgment(db, tmp_path, monkeypatch):
    prepare_env(db, monkeypatch)
    for name in ("IFLYTEK_ISE_APP_ID", "IFLYTEK_ISE_API_KEY", "IFLYTEK_ISE_API_SECRET"):
        monkeypatch.setenv(name, "fake-" + name)
    sockets = []

    def factory(*args, **kwargs):
        socket = FakeSocket("timeout")
        sockets.append(socket)
        return socket

    app = workbench.create_app(db, tmp_path, paid_enabled=True, ise_socket_factory=factory)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        login_admin(client)
        sid, tid, _ = submit_task(client, "ise", "repetition")
        root, query = f"/dev/qa/sessions/{sid}", f"?task_id={tid}"
        assert (
            client.post(
                root + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 200
        )
        assert client.post(root + "/run-candidate" + query).status_code == 200
        job = client.get(root + "/diagnostics" + query).json()["jobs"][0]
        assert job["status"] == "UNKNOWN" and len(sockets) == 1
        retry = root + f"/jobs/{job['job_id']}/retry" + query
        assert client.post(retry, json={"accept_unknown": False}).status_code == 409
        assert client.post(retry, json={"accept_unknown": True}).status_code == 200
        assert client.post(root + "/run-candidate" + query).status_code == 403
        assert len(sockets) == 1
