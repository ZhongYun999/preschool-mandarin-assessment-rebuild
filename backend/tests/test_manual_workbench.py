import importlib.util
import io
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app as production_app

spec = importlib.util.spec_from_file_location(
    "qa_workbench", Path(__file__).parent / "manual" / "workbench.py"
)
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


@pytest.mark.parametrize("media_type", ["audio/wav", "audio/webm"])
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
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 12345),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        assert c.get("/dev/qa").status_code == 200
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
            c.post(base + "/redo", headers=auth, json={"redo_request_id": "redo"}).status_code
            == 200
        )
        assert c.get(f"/dev/qa/sessions/{sid}/diagnostics").json()["submission"] is None
    assert not any(
        getattr(route, "path", "").startswith("/dev/qa") for route in production_app.routes
    )
