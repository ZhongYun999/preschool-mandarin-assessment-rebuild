"""Speaking help identities, checkpoint C and recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import UUID

import dashscope
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_speaking_process_asr import REQUIREMENT, FakeASR, ready, segment

from app.api.teacher_api import get_audio_store
from app.evaluation.worker import KnownFailure
from app.main import app
from app.media.fixed_audio import AudioData
from app.sessions.submission import Playback, finish_playback, start_playback
from app.task_types.speaking_asr_worker import claim as claim_asr
from app.task_types.speaking_asr_worker import execute as execute_asr
from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest
from app.task_types.speaking_guidance_worker import QwenGuidance, claim, execute, recover_expired
from app.task_types.speaking_tts_worker import run_audio

pytest_plugins = ("test_speaking_process_asr",)
CONTRACT = {"provider": "test", "model": "mock", "region": "cn-beijing", "workspace": "test"}


class FakeGuidance:
    def __init__(self, output=None):
        self.output = output if output is not None else {"guidance_text": "再说一些。"}
        self.calls = []

    def generate(self, prompt, context, contract):
        self.calls.append((prompt, context, contract))
        if self.output == "ERROR":
            raise KnownFailure("LLM_AUTHENTICATION")
        if self.output == "TIMEOUT":
            raise TimeoutError()
        return self.output


def help_request(client, base, h, aid, request_id="help"):
    return client.post(
        base + "/help-requests", headers=h, json={"attempt_id": aid, "request_id": request_id}
    )


def make_audio(db, help_id):
    import io
    import wave

    class Synth:
        def synthesize(self, source, config):
            out = io.BytesIO()
            with wave.open(out, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 160)
            return AudioData(out.getvalue(), "audio/wav")

    with Session(db) as s:
        audio = s.scalar(select(GuidanceAudio).where(GuidanceAudio.help_request_id == help_id))
        audio_id = audio.audio_id
    store = app.dependency_overrides[get_audio_store]()
    config = {
        "provider": "test",
        "model": "test",
        "voice": "test",
        "version": "1",
        "speed": 1,
        "sample_rate": 16000,
        "format": "wav",
    }
    assert run_audio(db, audio_id, store, Synth(), config) == "ready"
    return store


def simulate_delivery(db, root, base, aid, help_id, *, void=False):
    """Deliver using the real playback transaction, with an offline synthesizer."""
    sid, task_id = UUID(root.rsplit("/", 1)[1]), UUID(base.rsplit("/", 1)[1])
    store = make_audio(db, help_id)
    with Session(db) as s, s.begin():
        playback = start_playback(
            s,
            session_id=sid,
            session_task_id=task_id,
            attempt_id=UUID(aid),
            request_id=f"guidance-{help_id}",
            kind="GUIDANCE",
            help_request_id=help_id,
            store=store,
        )
        finish_playback(
            s,
            session_id=sid,
            session_task_id=task_id,
            attempt_id=UUID(aid),
            playback_id=playback.playback_id,
            completed=not void,
            store=store,
        )
        if void:
            row = s.get(SpeakingHelpRequest, help_id)
            row.status = "VOIDED"
            row.error_code = "DELIVERY_FAILED"
            row.execution_history = [*row.execution_history, {"status": "DELIVERY_FAILED"}]


def test_delivered_history_and_voided_generated_text_are_isolated(db, client, flow):
    root, base, h, aid = ready(client, flow)
    first = UUID(help_request(client, base, h, aid, "first").json()["help_request_id"])
    assert execute(
        db,
        claim(db, first, prompt="first prompt", contract=CONTRACT),
        FakeGuidance({"guidance_text": "第一条"}),
    )
    assert help_request(client, base, h, aid, "premature").status_code == 409
    with Session(db) as s:
        assert s.get(SpeakingHelpRequest, first).input_frontier["delivered_guidance"] == []
    simulate_delivery(db, root, base, aid, first)
    with db.begin() as conn:
        conn.execute(
            text("UPDATE speaking_help_requests SET status='DELIVERED' WHERE help_request_id=:id"),
            {"id": first},
        )
    actions = client.get(root, headers=h).json()["tasks"][0]["allowed_actions"]
    assert "START_RECORDING" in actions and "REQUEST_HELP" in actions
    second = UUID(help_request(client, base, h, aid, "second").json()["help_request_id"])
    assert second != first
    second_work = claim(db, second, prompt="second prompt", contract=CONTRACT)
    assert second_work.context["delivered_guidance"] == [
        {"help_request_id": str(first), "text": "第一条"}
    ]
    assert execute(db, second_work, FakeGuidance({"guidance_text": "未交付"}))
    with Session(db) as s:
        before = list(s.get(SpeakingHelpRequest, second).execution_history)
        second_frontier = dict(s.get(SpeakingHelpRequest, second).input_frontier)
    simulate_delivery(db, root, base, aid, second, void=True)
    with Session(db) as s:
        row = s.get(SpeakingHelpRequest, second)
        assert row.status == "VOIDED" and row.guidance_text == "未交付"
        assert (
            row.execution_history[: len(before)] == before and row.error_code == "DELIVERY_FAILED"
        )
    third = UUID(help_request(client, base, h, aid, "third").json()["help_request_id"])
    third_work = claim(db, third, prompt="third prompt", contract=CONTRACT)
    assert third_work.context["delivered_guidance"] == [
        {"help_request_id": str(first), "text": "第一条"}
    ]
    assert execute(db, third_work, FakeGuidance({"guidance_text": "第三条"}))
    simulate_delivery(db, root, base, aid, third)
    fourth = UUID(help_request(client, base, h, aid, "fourth").json()["help_request_id"])
    fourth_work = claim(db, fourth, prompt="fourth prompt", contract=CONTRACT)
    assert fourth_work.context["delivered_guidance"] == [
        {"help_request_id": str(first), "text": "第一条"},
        {"help_request_id": str(third), "text": "第三条"},
    ]
    with Session(db) as s:
        assert s.get(SpeakingHelpRequest, second).input_frontier == second_frontier
        assert [s.get(SpeakingHelpRequest, x).position for x in (first, second, third, fourth)] == [
            1,
            2,
            3,
            4,
        ]
    redo = client.post(
        base + "/redo", headers=h, json={"redo_request_id": "redo", "expected_attempt_id": aid}
    ).json()
    new = redo["attempt_id"]
    pid = client.post(
        base + "/playbacks",
        headers=h,
        json={"attempt_id": new, "request_id": "new-prompt", "kind": "INITIAL_PROMPT"},
    ).json()["playback_id"]
    assert (
        client.post(
            base + f"/playbacks/{pid}/finish",
            headers=h,
            json={"attempt_id": new, "completed": True},
        ).status_code
        == 200
    )
    new_help = UUID(help_request(client, base, h, new, "new").json()["help_request_id"])
    assert (
        claim(db, new_help, prompt="new prompt", contract=CONTRACT).context["delivered_guidance"]
        == []
    )


def test_empty_input_checkpoint_c_and_duplicate(db, client, flow):
    root, base, h, aid = ready(client, flow)
    assert "REQUEST_HELP" in client.get(root, headers=h).json()["tasks"][0]["allowed_actions"]
    response = help_request(client, base, h, aid)
    assert response.status_code == 200
    assert help_request(client, base, h, aid).json() == response.json()
    hid = UUID(response.json()["help_request_id"])
    assert help_request(client, base, h, aid, "different").status_code == 409
    assert (
        client.post(
            base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "blocked"}
        ).status_code
        == 409
    )
    work = claim(db, hid, prompt="prompt-v1", contract=CONTRACT)
    adapter = FakeGuidance()
    assert execute(db, work, adapter)
    assert not execute(db, work, adapter)
    context = adapter.calls[0][1]
    assert context["prompt_text"] == "题目甲" and context["grade_level"] == "测试学段"
    assert context["segments"] == [] and context["delivered_guidance"] == []
    assert "reference_answer" not in context and "scoring_instruction" not in context
    snapshot = client.get(root, headers=h)
    assert "prompt-v1" not in snapshot.text
    state = snapshot.json()["tasks"][0]
    assert state["speaking"]["phase"] == "DELIVERING_GUIDANCE"
    assert state["speaking"]["help_request"]["guidance_text"] == "再说一些。"
    assert state["speaking"]["help_request"]["checkpoint_c"]
    assert (
        "REQUEST_HELP" not in state["allowed_actions"]
        and "START_RECORDING" not in state["allowed_actions"]
    )
    with Session(db) as s:
        playbacks = list(s.scalars(select(Playback).where(Playback.attempt_id == UUID(aid))))
        assert len(playbacks) == 1 and playbacks[0].kind == "INITIAL_PROMPT"


@pytest.mark.parametrize(
    "change",
    [
        "status='DELIVERED', guidance_text='changed'",
        "status='VOIDED', guidance_text=NULL",
        "status='DELIVERED', execution_history='[]'::jsonb",
        "status='VOIDED', prompt_sha256='changed'",
    ],
)
def test_checkpoint_c_facts_cannot_be_rewritten_during_delivery(db, client, flow, change):
    _root, base, h, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as conn:
        conn.execute(
            text(f"UPDATE speaking_help_requests SET {change} WHERE help_request_id=:id"),
            {"id": help_id},
        )
    with Session(db) as s:
        row = s.get(SpeakingHelpRequest, help_id)
        assert row.status == "READY" and row.guidance_text == "再说一些。"
        assert len(row.execution_history) == 1


def test_failure_audit_history_is_append_only(db, client, flow):
    _root, base, h, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    assert execute(
        db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance("ERROR")
    )
    with pytest.raises(DBAPIError, match="immutable"), db.begin() as conn:
        conn.execute(
            text(
                "UPDATE speaking_help_requests SET execution_history='[]'::jsonb WHERE help_request_id=:id"
            ),
            {"id": help_id},
        )
    with Session(db) as s:
        assert (
            s.get(SpeakingHelpRequest, help_id).execution_history[0]["error_code"]
            == "LLM_AUTHENTICATION"
        )


def test_delivery_and_next_help_serialize(db, client, flow):
    import time

    root, base, h, aid = ready(client, flow)
    first = UUID(help_request(client, base, h, aid, "first").json()["help_request_id"])
    assert execute(db, claim(db, first, prompt="prompt", contract=CONTRACT), FakeGuidance())
    store = make_audio(db, first)
    with Session(db) as s, s.begin():
        playback = start_playback(
            s,
            session_id=UUID(root.rsplit("/", 1)[1]),
            session_task_id=UUID(base.rsplit("/", 1)[1]),
            attempt_id=UUID(aid),
            request_id="guidance",
            kind="GUIDANCE",
            help_request_id=first,
            store=store,
        )
        playback_id = playback.playback_id
    held, release, entering = Event(), Event(), Event()

    def deliver():
        with Session(db) as s, s.begin():
            finish_playback(
                s,
                session_id=UUID(root.rsplit("/", 1)[1]),
                session_task_id=UUID(base.rsplit("/", 1)[1]),
                attempt_id=UUID(aid),
                playback_id=playback_id,
                completed=True,
                store=store,
            )
            held.set()
            assert release.wait(10)

    def request_next():
        assert held.wait(10)
        entering.set()
        return help_request(client, base, h, aid, "next")

    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery, request = pool.submit(deliver), pool.submit(request_next)
        try:
            assert entering.wait(10)
            with db.connect() as conn:
                schema = conn.scalar(text("SELECT current_schema()"))
                deadline, blocked = time.monotonic() + 10, False
                while time.monotonic() < deadline:
                    blocked = conn.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name=:s AND cardinality(pg_blocking_pids(pid)) > 0)"
                        ),
                        {"s": schema},
                    )
                    conn.commit()
                    if blocked:
                        break
                    time.sleep(0.05)
                assert blocked
        finally:
            release.set()
        delivery.result(timeout=10)
        result = request.result(timeout=10)
    assert result.status_code == 200
    with Session(db) as s:
        row = s.get(SpeakingHelpRequest, UUID(result.json()["help_request_id"]))
        assert row.input_frontier["delivered_guidance"] == [
            {"help_request_id": str(first), "text": "再说一些。"}
        ]


@pytest.mark.parametrize("mode", ["TEXT", "NO_TEXT"])
def test_frozen_frontier_uses_confirmed_asr(db, client, flow, mode):
    root, base, h, aid = ready(client, flow)
    op, item = segment(client, root, base, h, aid, "rec")
    assert help_request(client, base, h, aid).status_code == 409
    assert execute_asr(db, claim_asr(db, op, REQUIREMENT), FakeASR(mode))
    response = help_request(client, base, h, aid)
    hid = UUID(response.json()["help_request_id"])
    work = claim(db, hid, prompt="prompt-v1", contract=CONTRACT)
    assert work.context["segments"][0]["asr_operation_id"] == str(op)
    assert work.context["segments"][0]["text"] == ("儿童实际说的话" if mode == "TEXT" else None)
    assert execute(db, work, FakeGuidance())
    with Session(db) as s:
        assert (
            s.get(SpeakingHelpRequest, hid).input_frontier["segments"][0]["segment_id"]
            == item["segment_id"]
        )


@pytest.mark.parametrize("output", [{}, {"guidance_text": " "}, "ERROR", "TIMEOUT"])
def test_failed_help_retry_or_void(db, client, flow, output):
    root, base, h, aid = ready(client, flow)
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    assert execute(db, claim(db, hid, prompt="prompt-v1", contract=CONTRACT), FakeGuidance(output))
    state = client.get(root, headers=h).json()["tasks"][0]
    assert not state["speaking"]["help_request"]["checkpoint_c"]
    assert "VOID_HELP" in state["allowed_actions"]
    url = base + f"/help-requests/{hid}/void"
    if output == "TIMEOUT":
        assert client.post(url, headers=h, json={"attempt_id": aid}).status_code == 409
    assert (
        client.post(
            url, headers=h, json={"attempt_id": aid, "accept_unknown": output == "TIMEOUT"}
        ).status_code
        == 200
    )
    state = client.get(root, headers=h).json()["tasks"][0]
    assert "REQUEST_HELP" in state["allowed_actions"]
    assert help_request(client, base, h, aid, "another").status_code == 200


def test_retry_uses_current_prompt_and_single_request(db, client, flow):
    _root, base, h, aid = ready(client, flow)
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    assert execute(db, claim(db, hid, prompt="old", contract=CONTRACT), FakeGuidance("ERROR"))
    body = {"attempt_id": aid, "request_id": "retry", "expected_attempts": 1}
    url = base + f"/help-requests/{hid}/retry"
    assert client.post(url, headers=h, json=body).status_code == 200
    assert client.post(url, headers=h, json=body).status_code == 200
    adapter = FakeGuidance()
    assert execute(db, claim(db, hid, prompt="new", contract=CONTRACT), adapter)
    assert adapter.calls[0][0] == "new"
    with Session(db) as s:
        history = s.get(SpeakingHelpRequest, hid).execution_history
        assert [entry["status"] for entry in history] == ["FAILED", "READY"]
        assert history[0]["error_code"] == "LLM_AUTHENTICATION"
        assert history[0]["prompt_sha256"] != history[1]["prompt_sha256"]


def test_concurrent_help_and_redo_isolation(db, client, flow):
    root, base, h, aid = ready(client, flow)
    barrier = Barrier(2)

    def request(key):
        barrier.wait(timeout=10)
        return help_request(client, base, h, aid, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ("a", "b")))
    assert sorted(r.status_code for r in results) == [200, 409]
    hid = UUID(next(r for r in results if r.status_code == 200).json()["help_request_id"])
    entered, release = Event(), Event()

    class Slow(FakeGuidance):
        def generate(self, prompt, context, contract):
            entered.set()
            assert release.wait(10)
            return super().generate(prompt, context, contract)

    work = claim(db, hid, prompt="prompt", contract=CONTRACT)
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
    assert client.get(root, headers=h).json()["tasks"][0]["speaking"]["help_request"] is None
    with Session(db) as s:
        assert s.get(SpeakingHelpRequest, hid).guidance_text == "再说一些。"


def test_help_and_recording_serialize(db, client, flow):
    root, base, h, aid = ready(client, flow)
    barrier = Barrier(2)

    def compete(kind):
        barrier.wait(timeout=10)
        if kind == "help":
            return help_request(client, base, h, aid)
        return client.post(
            base + "/recordings", headers=h, json={"attempt_id": aid, "request_id": "record"}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(compete, ("help", "record")))
    assert sorted(r.status_code for r in responses) == [200, 409]
    state = client.get(root, headers=h).json()["tasks"][0]
    assert not (state["recording_in_progress"] and state["speaking"]["help_request"] is not None)


def test_two_workers_claim_one_help(db, client, flow):
    _root, base, h, aid = ready(client, flow)
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    barrier = Barrier(2)

    def take(_):
        barrier.wait(timeout=10)
        return claim(db, hid, prompt="prompt", contract=CONTRACT)

    with ThreadPoolExecutor(max_workers=2) as pool:
        work = list(pool.map(take, range(2)))
    assert sum(item is not None for item in work) == 1
    assert execute(db, next(item for item in work if item), FakeGuidance())


def test_missing_configuration_can_be_voided(db, client, flow):
    from app.task_types.speaking_guidance_worker import fail_configuration

    root, base, h, aid = ready(client, flow)
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    assert fail_configuration(db, hid)
    state = client.get(root, headers=h).json()["tasks"][0]
    assert state["speaking"]["help_request"]["error_code"] == "GUIDANCE_CONFIGURATION_UNAVAILABLE"
    assert (
        client.post(
            base + f"/help-requests/{hid}/void", headers=h, json={"attempt_id": aid}
        ).status_code
        == 200
    )
    assert "REQUEST_HELP" in client.get(root, headers=h).json()["tasks"][0]["allowed_actions"]


@pytest.mark.parametrize("dispatched", [False, True])
def test_worker_crash_recovers_by_dispatch_boundary(db, client, flow, dispatched):
    _root, base, h, aid = ready(client, flow)
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    work = claim(db, hid, prompt="prompt", contract=CONTRACT, lease=timedelta(minutes=1))
    if dispatched:

        class Crashed(FakeGuidance):
            def generate(self, prompt, context, contract):
                raise SystemExit("simulated worker crash")

        with pytest.raises(SystemExit):
            execute(db, work, Crashed())
    with Session(db) as s, s.begin():
        s.execute(
            text(
                "UPDATE speaking_help_requests SET lease_until=clock_timestamp()-interval '1 second' WHERE help_request_id=:id"
            ),
            {"id": hid},
        )
        assert recover_expired(s) == 1
    with Session(db) as s:
        assert s.get(SpeakingHelpRequest, hid).status == ("UNKNOWN" if dispatched else "WAITING")
    assert not execute(db, work, FakeGuidance())
    if not dispatched:
        assert execute(db, claim(db, hid, prompt="prompt", contract=CONTRACT), FakeGuidance())


def test_frontier_contains_multiple_segments_in_order(db, client, flow):
    root, base, h, aid = ready(client, flow)
    identities = []
    for i in range(2):
        op, item = segment(client, root, base, h, aid, f"rec-{i}")
        identities.append((item["segment_id"], str(op)))
        assert execute_asr(
            db, claim_asr(db, op, REQUIREMENT), FakeASR("TEXT" if i == 0 else "NO_TEXT")
        )
    hid = UUID(help_request(client, base, h, aid).json()["help_request_id"])
    work = claim(db, hid, prompt="prompt", contract=CONTRACT)
    assert [
        (item["segment_id"], item["asr_operation_id"]) for item in work.context["segments"]
    ] == identities
    assert work.context["segments"][1]["text"] is None


@pytest.mark.parametrize(
    "content,valid",
    [
        ("{", False),
        ("{}", False),
        ('{"guidance_text":""}', False),
        ('{"guidance_text":"相同。"}', True),
    ],
)
def test_qwen_guidance_offline_json(monkeypatch, content, valid):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "output": {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ]
                }
            },
        )

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    adapter = QwenGuidance(client_factory=factory)
    if valid:
        assert adapter.generate("prompt", {}, CONTRACT)["guidance_text"] == "相同。"
    else:
        try:
            result = adapter.generate("prompt", {}, CONTRACT)
        except KnownFailure as exc:
            assert exc.code == "GUIDANCE_INVALID_RESPONSE"
        else:
            assert (
                not isinstance(result.get("guidance_text"), str)
                or not result["guidance_text"].strip()
            )


@pytest.mark.parametrize(
    "status,expected",
    [(401, "LLM_AUTHENTICATION"), (429, "LLM_RATE_LIMIT"), (400, "LLM_REQUEST_REJECTED")],
)
def test_qwen_http_errors_are_technical(monkeypatch, status, expected):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")

    def handler(request):
        return httpx.Response(status, content=b"not json")

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    with pytest.raises(KnownFailure, match=expected):
        QwenGuidance(client_factory=factory).generate("prompt", {}, CONTRACT)


@pytest.mark.parametrize(
    "status,body,expected", [(403, {"code": "Arrearage"}, "LLM_QUOTA"), (503, {}, "UNKNOWN")]
)
def test_qwen_quota_and_server_error_offline(monkeypatch, status, body, expected):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")

    def handler(request):
        return httpx.Response(status, json=body)

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    if expected == "UNKNOWN":
        with pytest.raises(RuntimeError, match="outcome unknown"):
            QwenGuidance(client_factory=factory).generate("prompt", {}, CONTRACT)
    else:
        with pytest.raises(KnownFailure, match=expected):
            QwenGuidance(client_factory=factory).generate("prompt", {}, CONTRACT)
