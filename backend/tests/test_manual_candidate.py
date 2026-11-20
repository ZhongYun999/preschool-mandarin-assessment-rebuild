"""Candidate workbench chain exercises only httpx.MockTransport; never real providers."""

import importlib.util
import io
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dashscope
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_dashscope_asr import body as asr_body
from test_qwen_qa_text import response as llm_body

spec = importlib.util.spec_from_file_location(
    "candidate_workbench", Path(__file__).parent / "manual" / "workbench.py"
)
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


@pytest.mark.parametrize("scenario", ["text", "no_text", "rejected", "timeout", "invalid"])
def test_candidate_chain_is_authorized_bounded_and_frozen(db, tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("VNEXT_PROVIDER_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unused-environment-fake")
    calls = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer old-fake-key"
        calls.append(request.url.path)
        if "multimodal" in request.url.path:
            if scenario == "rejected":
                return httpx.Response(401, json={"code": "InvalidApiKey"})
            if scenario == "timeout":
                raise httpx.ReadTimeout("simulated")
            return httpx.Response(200, json=asr_body("" if scenario == "no_text" else "测试文本"))
        return httpx.Response(
            200, json=llm_body("not json") if scenario == "invalid" else llm_body()
        )

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    app = workbench.create_app(db, tmp_path, paid_enabled=True, client_factory=factory)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        cfg = {
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "api_key": "old-fake-key",
            "retain_credential_id": None,
        }
        assert c.post("/dev/qa/provider-config", json=cfg).status_code == 200
        created = c.post("/dev/qa/sessions?mode=qwen&count=2")
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        cfg.update({"region": "us-east-1", "workspace": "new-workspace", "api_key": "new-fake-key"})
        assert c.post("/dev/qa/provider-config", json=cfg).status_code == 200
        token = c.post(f"/dev/qa/sessions/{sid}/token").json()["token"]
        auth = {"Authorization": "Bearer " + token}
        root = f"/assessment/sessions/{sid}"
        tasks = c.get(root, headers=auth).json()["tasks"]
        tid = tasks[0]["session_task_id"]
        base = f"{root}/tasks/{tid}"
        aid = c.post(base + "/attempt", headers=auth).json()["attempt_id"]
        playback = c.post(
            base + "/playbacks",
            headers=auth,
            json={"attempt_id": aid, "request_id": "play", "kind": "INITIAL_PROMPT"},
        ).json()
        assert (
            c.post(
                base + "/playbacks/" + playback["playback_id"] + "/finish",
                headers=auth,
                json={"attempt_id": aid, "completed": True},
            ).status_code
            == 200
        )
        rid = c.post(
            base + "/recordings", headers=auth, json={"attempt_id": aid, "request_id": "rec"}
        ).json()["recording_id"]
        data = io.BytesIO()
        with wave.open(data, "wb") as w:
            w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            w.writeframes(b"\x00\x00" * 1600)
        assert (
            c.post(
                base + "/recordings/" + rid + "/content",
                headers={**auth, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
                content=data.getvalue(),
            ).status_code
            == 200
        )
        submission_response = c.post(
            base + "/submissions/qa",
            headers=auth,
            json={"attempt_id": aid, "recording_id": rid, "submit_request_id": "sub"},
        )
        assert submission_response.status_code == 200, submission_response.text
        sub = submission_response.json()
        url = f"/dev/qa/sessions/{sid}"
        query = f"?task_id={tid}"
        assert c.post(url + "/run-candidate" + query).status_code == 403 and not calls
        assert c.post(url + "/run-offline" + query).status_code == 409 and not calls
        assert (
            c.post(
                url + "/authorize-paid" + query,
                json={"authorized_audio": False, "confirm_paid": True},
            ).status_code
            == 422
        )
        authorization = c.post(
            url + "/authorize-paid" + query, json={"authorized_audio": True, "confirm_paid": True}
        )
        assert authorization.status_code == 200, authorization.text
        assert (
            c.post(
                url + "/authorize-paid" + query,
                json={"authorized_audio": True, "confirm_paid": True},
            ).status_code
            == 409
        )
        if scenario == "text":
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: c.post(url + "/run-candidate" + query), range(3)))
            assert all(result.status_code == 200 for result in results)
        run = c.post(url + "/run-candidate" + query)
        assert run.status_code == 200, run.text
        diag = c.get(url + "/diagnostics" + query).json()
        assert diag["submission"]["submission_id"] == sub["submission_id"]
        assert diag["requirements"][0]["parameters"]["region"] == "cn-beijing"
        assert diag["requirements"][0]["parameters"]["workspace"] == "test-workspace"
        assert "old-fake-key" not in str(diag) and "new-fake-key" not in str(diag)
        statuses = [j["status"] for j in diag["jobs"]]
        expected = {
            "text": ["READY", "READY"],
            "no_text": ["READY", "WAITING"],
            "rejected": ["FAILED", "WAITING"],
            "timeout": ["UNKNOWN", "WAITING"],
            "invalid": ["READY", "INVALID"],
        }
        assert statuses == expected[scenario]
        assert len(calls) == (2 if scenario in ("text", "invalid") else 1)
        assert (
            c.post(
                url + "/jobs/" + diag["jobs"][0]["job_id"] + "/retry" + query,
                json={"accept_unknown": True},
            ).status_code
            == 409
        )
        assert c.post(url + "/run-candidate" + query).status_code == 200
        assert len(calls) == (2 if scenario in ("text", "invalid") else 1)
        if scenario == "text":
            restarted = workbench.create_app(
                db, tmp_path, paid_enabled=True, client_factory=factory
            )
            with TestClient(
                restarted,
                base_url="http://127.0.0.1:8000",
                client=("127.0.0.1", 1),
                headers={"Origin": "http://127.0.0.1:8000"},
            ) as after_restart:
                assert after_restart.post(url + "/run-candidate" + query).status_code == 200
            assert len(calls) == 2
        assert (
            c.get(url + f"/diagnostics?task_id={tasks[1]['session_task_id']}").json()["submission"]
            is None
        )
        if scenario == "text":
            tid2 = tasks[1]["session_task_id"]
            base2, query2 = f"{root}/tasks/{tid2}", f"?task_id={tid2}"
            aid2 = c.post(base2 + "/attempt", headers=auth).json()["attempt_id"]
            play2 = c.post(
                base2 + "/playbacks",
                headers=auth,
                json={"attempt_id": aid2, "request_id": "play", "kind": "INITIAL_PROMPT"},
            ).json()
            assert (
                c.post(
                    base2 + "/playbacks/" + play2["playback_id"] + "/finish",
                    headers=auth,
                    json={"attempt_id": aid2, "completed": True},
                ).status_code
                == 200
            )
            rid2 = c.post(
                base2 + "/recordings", headers=auth, json={"attempt_id": aid2, "request_id": "rec"}
            ).json()["recording_id"]
            assert (
                c.post(
                    base2 + "/recordings/" + rid2 + "/content",
                    headers={**auth, "X-Attempt-Id": aid2, "Content-Type": "audio/wav"},
                    content=data.getvalue(),
                ).status_code
                == 200
            )
            sub2 = c.post(
                base2 + "/submissions/qa",
                headers=auth,
                json={"attempt_id": aid2, "recording_id": rid2, "submit_request_id": "sub"},
            ).json()
            assert sub2["submission_id"] != sub["submission_id"] and aid2 != aid
            assert (
                c.post(
                    url + "/authorize-paid" + query2,
                    json={"authorized_audio": True, "confirm_paid": True},
                ).status_code
                == 200
            )
            assert c.post(url + "/run-candidate" + query2).status_code == 200
            diag2 = c.get(url + "/diagnostics" + query2).json()
            assert all(
                j["input_refs"]["submission_id"] == sub2["submission_id"] for j in diag2["jobs"]
            )
            assert diag2["jobs"][0]["evidence"]["id"] != diag["jobs"][0]["evidence"]["id"]
            assert (
                c.post(base2 + "/redo", headers=auth, json={"redo_request_id": "redo"}).status_code
                == 200
            )
            assert c.get(url + "/diagnostics" + query2).json()["submission"] is None
            assert c.get(url + "/diagnostics" + query).json() == diag
            next_attempt = c.get(root, headers=auth).json()["tasks"][1]["attempt_id"]
            assert next_attempt != aid2
            next_play = c.post(
                base2 + "/playbacks",
                headers=auth,
                json={"attempt_id": next_attempt, "request_id": "play2", "kind": "INITIAL_PROMPT"},
            ).json()
            assert (
                c.post(
                    base2 + "/playbacks/" + next_play["playback_id"] + "/finish",
                    headers=auth,
                    json={"attempt_id": next_attempt, "completed": True},
                ).status_code
                == 200
            )
            next_rid = c.post(
                base2 + "/recordings",
                headers=auth,
                json={"attempt_id": next_attempt, "request_id": "rec2"},
            ).json()["recording_id"]
            assert (
                c.post(
                    base2 + "/recordings/" + next_rid + "/content",
                    headers={**auth, "X-Attempt-Id": next_attempt, "Content-Type": "audio/wav"},
                    content=data.getvalue(),
                ).status_code
                == 200
            )
            next_sub = c.post(
                base2 + "/submissions/qa",
                headers=auth,
                json={
                    "attempt_id": next_attempt,
                    "recording_id": next_rid,
                    "submit_request_id": "sub2",
                },
            ).json()
            assert c.post(url + "/run-candidate" + query2).status_code == 403
            assert (
                c.post(
                    url + "/authorize-paid" + query2,
                    json={"authorized_audio": True, "confirm_paid": True},
                ).status_code
                == 200
            )
            assert c.post(url + "/run-candidate" + query2).status_code == 200
            replacement = c.get(url + "/diagnostics" + query2).json()
            assert all(
                j["input_refs"]["submission_id"] == next_sub["submission_id"]
                for j in replacement["jobs"]
            )
            assert replacement["jobs"][0]["evidence"]["id"] != diag2["jobs"][0]["evidence"]["id"]
            assert c.get(url + "/diagnostics" + query).json() == diag


def test_default_server_never_authorizes_or_dispatches(db, tmp_path, monkeypatch):
    monkeypatch.setenv("VNEXT_PROVIDER_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-environment-secret")

    def forbidden(**kwargs):
        raise AssertionError("No provider client may be created")

    app = workbench.create_app(db, tmp_path, client_factory=forbidden)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        assert (
            c.post(
                "/dev/qa/provider-config",
                json={
                    "region": "cn-beijing",
                    "workspace": "test-workspace",
                    "api_key": "fake-database-secret",
                    "retain_credential_id": None,
                },
            ).status_code
            == 200
        )
        sid = c.post("/dev/qa/sessions?mode=qwen").json()["session_id"]
        root = f"/dev/qa/sessions/{sid}"
        assert (
            c.post(
                root + "/authorize-paid", json={"authorized_audio": True, "confirm_paid": True}
            ).status_code
            == 403
        )
        assert c.post(root + "/run-candidate").status_code == 403
