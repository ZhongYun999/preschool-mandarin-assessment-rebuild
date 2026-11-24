from uuid import UUID

from sqlalchemy.orm import Session
from test_speaking_guidance import CONTRACT, FakeGuidance, help_request, make_audio
from test_speaking_process_asr import REQUIREMENT, FakeASR, ready, segment

from app.task_types.speaking_asr_worker import claim as claim_asr
from app.task_types.speaking_asr_worker import execute as execute_asr
from app.task_types.speaking_guidance import SpeakingHelpRequest
from app.task_types.speaking_guidance_worker import claim, execute

pytest_plugins = ("test_speaking_process_asr",)


def test_guidance_failure_retry_completion_and_next_help(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    make_audio(db, help_id)
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["effective_help_count"] == 0
    assert "START_GUIDANCE_PLAYBACK" in state["allowed_actions"]
    payload = {
        "attempt_id": aid,
        "request_id": "guidance-1",
        "kind": "GUIDANCE",
        "help_request_id": str(help_id),
    }
    started = client.post(base + "/playbacks", headers=headers, json=payload)
    assert started.status_code == 200
    first = started.json()
    assert client.get(first["audio_url"], headers=headers).status_code == 200
    assert (
        client.get(root, headers=headers).json()["tasks"][0]["speaking"]["effective_help_count"]
        == 0
    )
    assert (
        client.post(
            base + "/playbacks/" + first["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    retry = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "retry-1",
            "kind": "TECHNICAL_RETRY",
            "retry_of_id": first["playback_id"],
            "help_request_id": str(help_id),
        },
    )
    assert retry.status_code == 200
    assert retry.json()["guidance_audio_id"] == first["guidance_audio_id"]
    url = base + "/playbacks/" + retry.json()["playback_id"] + "/finish"
    assert (
        client.post(url, headers=headers, json={"attempt_id": aid, "completed": True}).status_code
        == 200
    )
    assert (
        client.post(url, headers=headers, json={"attempt_id": aid, "completed": True}).status_code
        == 200
    )
    assert (
        client.post(url, headers=headers, json={"attempt_id": aid, "completed": False}).status_code
        == 409
    )
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["effective_help_count"] == 1
    assert (
        "START_RECORDING" in state["allowed_actions"] and "REQUEST_HELP" in state["allowed_actions"]
    )
    asr_id, _ = segment(client, root, base, headers, aid, "after-guidance")
    assert execute_asr(db, claim_asr(db, asr_id, REQUIREMENT), FakeASR())
    next_help = help_request(client, base, headers, aid, "next")
    assert next_help.status_code == 200
    with Session(db) as session:
        item = session.get(SpeakingHelpRequest, UUID(next_help.json()["help_request_id"]))
        assert item.input_frontier["delivered_guidance"] == [
            {"help_request_id": str(help_id), "text": "再说一些。"}
        ]


def test_unknown_requires_explicit_authorization(db, client, flow, tmp_path):
    from sqlalchemy import select

    from app.media.dashscope_tts import TTSError
    from app.media.fixed_audio import AudioStore
    from app.task_types.speaking_guidance import GuidanceAudio
    from app.task_types.speaking_tts_worker import run_audio

    class Timeout:
        calls = 0

        def synthesize(self, source, config):
            self.calls += 1
            raise TTSError("timeout", uncertain=True)

    _, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    with Session(db) as session:
        audio_id = session.scalar(
            select(GuidanceAudio.audio_id).where(GuidanceAudio.help_request_id == help_id)
        )
    config = {
        "provider": "test",
        "model": "test",
        "voice": "test",
        "version": "1",
        "speed": 1,
        "sample_rate": 16000,
        "format": "wav",
    }
    synth = Timeout()
    assert run_audio(db, audio_id, AudioStore(tmp_path), synth, config) == "unknown"
    assert run_audio(db, audio_id, AudioStore(tmp_path), synth, config) == "unknown"
    assert synth.calls == 1
    url = base + "/help-requests/" + str(help_id) + "/audio/retry"
    payload = {"attempt_id": aid, "request_id": "retry", "expected_attempts": 1}
    assert client.post(url, headers=headers, json=payload).status_code == 409
    assert (
        client.post(url, headers=headers, json={**payload, "accept_unknown": True}).status_code
        == 200
    )
    assert (
        client.post(url, headers=headers, json={**payload, "accept_unknown": True}).status_code
        == 200
    )


def test_tts_concurrency_and_redo_isolation(db, client, flow, tmp_path):
    import io
    import wave
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import select

    from app.media.fixed_audio import AudioData, AudioStore
    from app.task_types.speaking_guidance import GuidanceAudio
    from app.task_types.speaking_tts_worker import run_audio

    entered, release = Event(), Event()

    class Blocking:
        calls = 0

        def synthesize(self, source, config):
            self.calls += 1
            entered.set()
            assert release.wait(10)
            data = io.BytesIO()
            with wave.open(data, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 80)
            return AudioData(data.getvalue(), "audio/wav")

    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    with Session(db) as session:
        audio_id = session.scalar(
            select(GuidanceAudio.audio_id).where(GuidanceAudio.help_request_id == help_id)
        )
    config = {
        "provider": "test",
        "model": "test",
        "voice": "test",
        "version": "1",
        "speed": 1,
        "sample_rate": 16000,
        "format": "wav",
    }
    store, synth = AudioStore(tmp_path), Blocking()
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(run_audio, db, audio_id, store, synth, config)
        try:
            assert entered.wait(10)
            assert run_audio(db, audio_id, store, synth, config) == "busy"
            redo = client.post(
                base + "/redo",
                headers=headers,
                json={"redo_request_id": "redo", "expected_attempt_id": aid},
            )
            assert redo.status_code == 200
            assert redo.json()["attempt_id"] != aid
        finally:
            release.set()
        assert work.result(timeout=10) == "ready"
    assert synth.calls == 1
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["effective_help_count"] == 0
    assert state["speaking"]["help_request"] is None


def test_delivery_rollback_and_forged_delivery(db, client, flow):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.sessions.submission import finish_playback, start_playback

    root, base, headers, aid = ready(client, flow)
    sid, task_id = UUID(root.rsplit("/", 1)[1]), UUID(base.rsplit("/", 1)[1])
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    with pytest.raises(DBAPIError), db.begin() as connection:
        connection.execute(
            text("UPDATE speaking_help_requests SET status='DELIVERED' WHERE help_request_id=:id"),
            {"id": help_id},
        )
    store = make_audio(db, help_id)
    with Session(db) as session, session.begin():
        playback = start_playback(
            session,
            session_id=sid,
            session_task_id=task_id,
            attempt_id=UUID(aid),
            request_id="delivery",
            kind="GUIDANCE",
            help_request_id=help_id,
            store=store,
        )
        playback_id = playback.playback_id
    with pytest.raises(RuntimeError), Session(db) as session, session.begin():
        finish_playback(
            session,
            session_id=sid,
            session_task_id=task_id,
            attempt_id=UUID(aid),
            playback_id=playback_id,
            completed=True,
            store=store,
        )
        raise RuntimeError("simulate transaction failure")
    with Session(db) as session:
        from app.sessions.submission import Playback

        assert session.get(Playback, playback_id).status == "STARTED"
        assert session.get(SpeakingHelpRequest, help_id).status == "READY"


def test_saved_audio_resumes_without_resynthesis(db, client, flow, tmp_path):
    import io
    import wave

    import pytest
    from sqlalchemy import select

    from app.media.fixed_audio import AudioData, AudioStore
    from app.task_types.speaking_guidance import GuidanceAudio
    from app.task_types.speaking_tts_worker import run_audio

    class OnceBrokenStore(AudioStore):
        reads = 0

        def read(self, ref, digest):
            self.reads += 1
            if self.reads == 2:
                raise OSError("simulated crash after durable save")
            return super().read(ref, digest)

    class Synth:
        calls = 0

        def synthesize(self, source, config):
            self.calls += 1
            out = io.BytesIO()
            with wave.open(out, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 80)
            return AudioData(out.getvalue(), "audio/wav")

    _, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    with Session(db) as session:
        audio_id = session.scalar(
            select(GuidanceAudio.audio_id).where(GuidanceAudio.help_request_id == help_id)
        )
    config = {
        "provider": "test",
        "model": "test",
        "voice": "test",
        "version": "1",
        "speed": 1,
        "sample_rate": 16000,
        "format": "wav",
    }
    store, synth = OnceBrokenStore(tmp_path), Synth()
    with pytest.raises(OSError):
        run_audio(db, audio_id, store, synth, config)
    with Session(db) as session:
        assert session.get(GuidanceAudio, audio_id).status == "SAVED"
    assert run_audio(db, audio_id, store, synth, config) == "ready"
    assert synth.calls == 1


def test_three_successful_help_requests_are_counted_once(db, client, flow):
    from test_speaking_guidance import simulate_delivery

    root, base, headers, aid = ready(client, flow)
    ids = []
    for index in range(3):
        help_id = UUID(
            help_request(client, base, headers, aid, f"help-{index}").json()["help_request_id"]
        )
        work = claim(db, help_id, prompt="prompt", contract=CONTRACT)
        assert [item["help_request_id"] for item in work.context["delivered_guidance"]] == [
            str(item) for item in ids
        ]
        assert execute(db, work, FakeGuidance())
        simulate_delivery(db, root, base, aid, help_id)
        ids.append(help_id)
        state = client.get(root, headers=headers).json()["tasks"][0]["speaking"]
        assert state["effective_help_count"] == index + 1


def test_missing_audio_and_late_completion_after_redo(db, client, flow):
    from sqlalchemy import select

    from app.task_types.speaking_guidance import GuidanceAudio

    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    store = make_audio(db, help_id)
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "guidance",
            "kind": "GUIDANCE",
            "help_request_id": str(help_id),
        },
    ).json()
    with Session(db) as session:
        audio = session.scalar(
            select(GuidanceAudio).where(GuidanceAudio.help_request_id == help_id)
        )
        path = store.root / audio.storage_ref
    path.unlink()
    finish = base + "/playbacks/" + playback["playback_id"] + "/finish"
    assert (
        client.post(
            finish, headers=headers, json={"attempt_id": aid, "completed": True}
        ).status_code
        == 409
    )
    assert (
        client.get(root, headers=headers).json()["tasks"][0]["speaking"]["effective_help_count"]
        == 0
    )
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": aid},
    )
    assert redo.status_code == 200
    assert (
        client.post(
            finish, headers=headers, json={"attempt_id": aid, "completed": True}
        ).status_code
        == 409
    )
    assert (
        client.get(root, headers=headers).json()["tasks"][0]["speaking"]["effective_help_count"]
        == 0
    )


def test_completion_and_redo_are_serialized(db, client, flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    make_audio(db, help_id)
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "guidance",
            "kind": "GUIDANCE",
            "help_request_id": str(help_id),
        },
    ).json()
    barrier = Barrier(2)

    def finish():
        barrier.wait(timeout=10)
        return client.post(
            base + "/playbacks/" + playback["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": True},
        )

    def redo():
        barrier.wait(timeout=10)
        return client.post(
            base + "/redo",
            headers=headers,
            json={"redo_request_id": "redo", "expected_attempt_id": aid},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion, replacement = pool.submit(finish), pool.submit(redo)
        result, replaced = completion.result(timeout=15), replacement.result(timeout=15)
    assert replaced.status_code == 200 and result.status_code in (200, 409)
    with Session(db) as session:
        assert session.get(SpeakingHelpRequest, help_id).status == (
            "DELIVERED" if result.status_code == 200 else "READY"
        )
    assert (
        client.get(root, headers=headers).json()["tasks"][0]["speaking"]["effective_help_count"]
        == 0
    )


def test_failed_delivery_can_be_voided_without_erasing_c(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    make_audio(db, help_id)
    started = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "guidance",
            "kind": "GUIDANCE",
            "help_request_id": str(help_id),
        },
    )
    playback_id = started.json()["playback_id"]
    url = base + "/help-requests/" + str(help_id) + "/void"
    assert client.post(url, headers=headers, json={"attempt_id": aid}).status_code == 409
    assert (
        client.post(
            base + "/playbacks/" + playback_id + "/finish",
            headers=headers,
            json={"attempt_id": aid, "completed": False},
        ).status_code
        == 200
    )
    assert client.post(url, headers=headers, json={"attempt_id": aid}).status_code == 200
    with Session(db) as session:
        row = session.get(SpeakingHelpRequest, help_id)
        assert row.status == "VOIDED" and row.guidance_text == "再说一些。"
        assert row.execution_history[-1]["status"] == "DELIVERY_FAILED"
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["effective_help_count"] == 0
    assert "REQUEST_HELP" in state["allowed_actions"]


def test_duplicate_completion_requests_are_serialized(db, client, flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    make_audio(db, help_id)
    started = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": "guidance",
            "kind": "GUIDANCE",
            "help_request_id": str(help_id),
        },
    )
    url = base + "/playbacks/" + started.json()["playback_id"] + "/finish"
    barrier = Barrier(2)

    def finish():
        barrier.wait(timeout=10)
        return client.post(url, headers=headers, json={"attempt_id": aid, "completed": True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(finish) for _ in range(2)]
        assert [job.result(timeout=15).status_code for job in jobs] == [200, 200]
    assert (
        client.get(root, headers=headers).json()["tasks"][0]["speaking"]["effective_help_count"]
        == 1
    )
