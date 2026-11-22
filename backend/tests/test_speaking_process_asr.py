"""Process ASR drives Checkpoint B without a Submission."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import dashscope
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_qa_http import wav

from app.api.assessment_repetition import get_recording_store
from app.evaluation.asr_contract import MODEL, SOURCE, VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.worker import AdapterResult, KnownFailure
from app.main import app
from app.media.audio_inspection import AudioTools
from app.media.recording_store import RecordingStore
from app.task_types.speaking_asr import SpeakingASR
from app.task_types.speaking_asr_worker import claim, execute, recover_expired

pytest_plugins = ("test_speaking_checkpoint_a",)
REQUIREMENT = {
    "source": "test-asr",
    "version": "1",
    "parameters": {"region": "test", "workspace": "test"},
}


class FakeASR:
    confirmed_no_speech = True

    def __init__(self, mode="TEXT"):
        self.mode = mode
        self.calls = 0

    def evaluate(self, inputs, requirement):
        self.calls += 1
        if self.mode == "TIMEOUT":
            raise TimeoutError()
        if self.mode == "ERROR":
            raise KnownFailure("ASR_AUTHENTICATION")
        return AdapterResult(
            payload={
                "status": self.mode,
                "text": "儿童实际说的话" if self.mode == "TEXT" else None,
                "request_id": "mock-request",
            },
            provider="test",
            model="test-asr",
        )

    def validate_result(self, result, requirement):
        return result.payload["status"] in ("TEXT", "NO_TEXT")


def ready(client, flow):
    root, base, h, aid = flow(None)
    p = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": aid, "request_id": "p", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{p}/finish", headers=h, json={"attempt_id": aid, "completed": True}
        ).status_code
        == 200
    )
    return root, base, h, aid


def segment(client, root, base, h, aid, req):
    rid = client.post(
        base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": req}
    ).json()["recording_id"]
    result = client.post(
        base + f"/recordings/{rid}/content",
        headers={**h, "X-Attempt-Id": aid, "Content-Type": "audio/wav"},
        content=wav(),
    )
    assert result.status_code == 200
    state = client.get(root, headers=h).json()["tasks"][0]
    item = state["speaking"]["segments"][-1]
    return UUID(item["asr"]["operation_id"]), item


@pytest.mark.parametrize("mode", ["TEXT", "NO_TEXT"])
def test_three_segments_and_checkpoint_b(db, client, flow, mode):
    root, base, h, aid = ready(client, flow)
    for i in range(3):
        job, item = segment(client, root, base, h, aid, f"rec-{i}")
        assert item["position"] == i + 1
        assert (
            client.post(
                base + "/recordings",
                headers=h,
                json={"attempt_id": aid, "request_id": f"blocked-{i}"},
            ).status_code
            == 409
        )
        work = claim(db, job, REQUIREMENT)
        adapter = FakeASR(mode)
        assert execute(db, work, adapter)
        assert not execute(db, work, adapter)
        state = client.get(root, headers=h).json()["tasks"][0]
        assert state["speaking"]["checkpoint_b"]
        assert "START_RECORDING" in state["allowed_actions"]
        assert state["speaking"]["segments"][-1]["asr"]["semantic"] == (
            "TRANSCRIBED" if mode == "TEXT" else "NO_RECOGNIZED_SPEECH"
        )
    assert state["speaking"]["total_duration_seconds"] == pytest.approx(300 / 16000)


@pytest.mark.parametrize("mode", ["TIMEOUT", "ERROR", "INVALID"])
def test_failure_retry_without_rerecording(db, client, flow, mode):
    root, base, h, aid = ready(client, flow)
    job, item = segment(client, root, base, h, aid, "rec")
    assert execute(db, claim(db, job, REQUIREMENT), FakeASR(mode))
    state = client.get(root, headers=h).json()["tasks"][0]
    assert not state["speaking"]["checkpoint_b"]
    assert "RETRY_CURRENT_ASR" in state["allowed_actions"]
    body = {
        "attempt_id": aid,
        "request_id": "retry",
        "expected_attempts": 1,
        "accept_unknown": mode == "TIMEOUT",
    }
    endpoint = base + "/segments/" + item["segment_id"] + "/asr/retry"
    if mode == "TIMEOUT":
        assert (
            client.post(endpoint, headers=h, json={**body, "accept_unknown": False}).status_code
            == 409
        )
    assert client.post(endpoint, headers=h, json=body).status_code == 200
    assert client.post(endpoint, headers=h, json=body).status_code == 200
    assert execute(db, claim(db, job, REQUIREMENT), FakeASR())
    state = client.get(root, headers=h).json()["tasks"][0]
    assert len(state["speaking"]["segments"]) == 1 and state["speaking"]["checkpoint_b"]


def test_worker_competition_and_expired_claim(db, client, flow):
    root, base, h, aid = ready(client, flow)
    job, _ = segment(client, root, base, h, aid, "rec")
    barrier = Barrier(2)

    def take(_):
        barrier.wait(timeout=10)
        return claim(db, job, REQUIREMENT, timedelta(minutes=5))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(take, range(2)))
    assert sum(c is not None for c in claims) == 1
    with Session(db) as s, s.begin():
        s.execute(
            text(
                "UPDATE speaking_asr_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=:id"
            ),
            {"id": job},
        )
        assert recover_expired(s) == 1
    fresh = claim(db, job, REQUIREMENT)
    assert execute(db, fresh, FakeASR())
    assert not execute(db, next(c for c in claims if c is not None), FakeASR("ERROR"))
    with Session(db) as s:
        assert s.get(SpeakingASR, job).status == "READY"


def test_redo_before_dispatch_prevents_external_call(db, client, flow):
    root, base, h, aid = ready(client, flow)
    job, _ = segment(client, root, base, h, aid, "rec")
    work = claim(db, job, REQUIREMENT)
    assert (
        client.post(
            base + "/redo", headers=h, json={"redo_request_id": "redo", "expected_attempt_id": aid}
        ).status_code
        == 200
    )
    adapter = FakeASR()
    assert not execute(db, work, adapter) and adapter.calls == 0
    assert client.get(root, headers=h).json()["tasks"][0]["speaking"]["segments"] == []
    with Session(db) as s:
        assert s.get(SpeakingASR, job).status == "WAITING"


def test_changed_worker_input_never_dispatched(db, client, flow):
    root, base, h, aid = ready(client, flow)
    job, _ = segment(client, root, base, h, aid, "rec")
    work = claim(db, job, REQUIREMENT)
    work.recording["sha256"] = "0" * 64
    adapter = FakeASR()
    assert not execute(db, work, adapter) and adapter.calls == 0
    with Session(db) as s:
        current = s.get(SpeakingASR, job)
        assert current.status == "FAILED" and current.error_code == "INPUT_IDENTITY_MISMATCH"


def test_crash_after_dispatch_is_unknown_and_requires_ack(db, client, flow):
    root, base, h, aid = ready(client, flow)
    job, item = segment(client, root, base, h, aid, "rec")
    work = claim(db, job, REQUIREMENT)

    class Crashed(FakeASR):
        def evaluate(self, inputs, requirement):
            raise SystemExit("simulated process crash")

    with pytest.raises(SystemExit):
        execute(db, work, Crashed())
    with Session(db) as s, s.begin():
        s.execute(
            text(
                "UPDATE speaking_asr_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=:id"
            ),
            {"id": job},
        )
        assert recover_expired(s) == 1
    with Session(db) as s:
        assert s.get(SpeakingASR, job).status == "UNKNOWN"
    endpoint = base + "/segments/" + item["segment_id"] + "/asr/retry"
    body = {"attempt_id": aid, "request_id": "retry", "expected_attempts": 1}
    assert client.post(endpoint, headers=h, json=body).status_code == 409
    assert claim(db, job, REQUIREMENT) is None
    assert (
        client.post(endpoint, headers=h, json={**body, "accept_unknown": True}).status_code == 200
    )
    assert execute(db, claim(db, job, REQUIREMENT), FakeASR())


def test_late_result_stays_with_old_attempt(db, client, flow):
    from threading import Event

    root, base, h, aid = ready(client, flow)
    job, _ = segment(client, root, base, h, aid, "rec")
    work, entered, release = claim(db, job, REQUIREMENT), Event(), Event()

    class Slow(FakeASR):
        def evaluate(self, inputs, requirement):
            entered.set()
            assert release.wait(10)
            return super().evaluate(inputs, requirement)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, db, work, Slow())
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
        assert future.result(timeout=10)
    assert client.get(root, headers=h).json()["tasks"][0]["speaking"]["segments"] == []
    with Session(db) as s:
        assert s.get(SpeakingASR, job).status == "READY"
        assert str(s.get(SpeakingASR, job).attempt_id) == aid


@pytest.mark.parametrize("recognized", ["实际识别文本", ""])
def test_real_qwen_adapter_with_offline_transport(
    db, client, flow, monkeypatch, tmp_path, recognized
):
    from test_dashscope_asr import body

    root, base, h, aid = ready(client, flow)
    job, _ = segment(client, root, base, h, aid, "rec")
    tools = AudioTools()
    requirement = {
        "source": SOURCE,
        "version": VERSION,
        "parameters": {
            "model": MODEL,
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "enable_itn": True,
            "language": "zh",
            "audio_spec": {
                "container": "wav",
                "codec": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "processing_version": "test",
                "ffmpeg_version": tools.version(),
                "ffprobe_version": tools.version(probe=True),
            },
        },
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body(recognized))

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")
    adapter = DashScopeASR(
        app.dependency_overrides[get_recording_store](),
        RecordingStore(tmp_path / "derived"),
        tools,
        client_factory=factory,
    )
    assert execute(db, claim(db, job, requirement), adapter)
    assert len(calls) == 1
    info = client.get(root, headers=h).json()["tasks"][0]["speaking"]["segments"][0]["asr"]
    if recognized:
        assert info["status"] == "READY" and info["text"] == recognized
    else:
        assert info["status"] == "FAILED" and info["error_code"] == "ASR_UNCONFIRMED_RESULT"
