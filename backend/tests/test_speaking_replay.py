"""Speaking USER_REPLAY uses frozen playback facts and current delivery history."""

from uuid import UUID

import pytest
from sqlalchemy.orm import Session
from test_speaking_guidance import (
    CONTRACT,
    FakeGuidance,
    help_request,
    simulate_delivery,
)
from test_speaking_process_asr import ready

from app.task_types.speaking_guidance import SpeakingHelpRequest
from app.task_types.speaking_guidance_worker import claim, execute

pytest_plugins = ("test_speaking_process_asr",)


def start(client, base, headers, aid, request_id, **extra):
    return client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": aid, "request_id": request_id, "kind": "USER_REPLAY", **extra},
    )


def finish(client, base, headers, aid, playback, complete):
    return client.post(
        base + "/playbacks/" + playback["playback_id"] + "/finish",
        headers=headers,
        json={"attempt_id": aid, "completed": complete},
    )


def retry(client, base, headers, aid, original, request_id):
    return client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": aid,
            "request_id": request_id,
            "kind": "TECHNICAL_RETRY",
            "retry_of_id": original["playback_id"],
        },
    )


def test_initial_replay_idempotence_and_serial_operation(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_target"]["source"] == "INITIAL_PROMPT"
    assert state["speaking"]["replay_count"] == 0
    assert "START_USER_REPLAY" in state["allowed_actions"]
    response = start(client, base, headers, aid, "replay")
    assert response.status_code == 200
    original = response.json()
    assert original["audio_id"] and not original["guidance_audio_id"]
    assert (
        start(client, base, headers, aid, "replay").json()["playback_id"] == original["playback_id"]
    )
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert (
        state["speaking"]["phase"] == "REPLAYING_PROMPT" and state["speaking"]["replay_count"] == 0
    )
    assert (
        "FINISH_PLAYBACK" in state["allowed_actions"]
        and "START_RECORDING" not in state["allowed_actions"]
    )
    assert start(client, base, headers, aid, "another").status_code == 409
    assert (
        client.post(
            base + "/recordings", headers=headers, json={"attempt_id": aid, "request_id": "record"}
        ).status_code
        == 409
    )
    assert help_request(client, base, headers, aid).status_code == 409
    assert finish(client, base, headers, aid, original, True).status_code == 200
    assert finish(client, base, headers, aid, original, True).status_code == 200
    assert finish(client, base, headers, aid, original, False).status_code == 409
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_count"] == 1 and state["speaking"]["effective_help_count"] == 0
    assert "START_USER_REPLAY" in state["allowed_actions"]


def test_dynamic_replay_retries_and_later_guidance(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    first = UUID(help_request(client, base, headers, aid, "first").json()["help_request_id"])
    assert execute(
        db,
        claim(db, first, prompt="prompt", contract=CONTRACT),
        FakeGuidance({"guidance_text": "第一条"}),
    )
    assert start(client, base, headers, aid, "premature").status_code == 409
    simulate_delivery(db, root, base, aid, first)
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_target"]["help_request_id"] == str(first)
    response = start(client, base, headers, aid, "replay-1")
    assert response.status_code == 200
    original = response.json()
    assert original["guidance_audio_id"] and original["audio_id"] is None
    assert (
        start(client, base, headers, aid, "replay-1").json()["playback_id"]
        == original["playback_id"]
    )
    assert finish(client, base, headers, aid, original, False).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]["speaking"]
    assert state["replay_count"] == 0
    assert any(
        p["playback_id"] == original["playback_id"] and p["status"] == "FAILED"
        for p in state["replay_playbacks"]
    )
    attempt1 = retry(client, base, headers, aid, original, "retry-1")
    assert (
        attempt1.status_code == 200
        and attempt1.json()["guidance_audio_id"] == original["guidance_audio_id"]
    )
    assert finish(client, base, headers, aid, attempt1.json(), False).status_code == 200
    attempt2 = retry(client, base, headers, aid, original, "retry-2")
    assert attempt2.status_code == 200
    assert finish(client, base, headers, aid, attempt2.json(), True).status_code == 200
    assert retry(client, base, headers, aid, original, "retry-3").status_code == 409
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_count"] == 1 and state["speaking"]["effective_help_count"] == 1
    assert "RETRY_PLAYBACK" not in state["allowed_actions"]
    second = UUID(help_request(client, base, headers, aid, "second").json()["help_request_id"])
    assert execute(
        db,
        claim(db, second, prompt="prompt", contract=CONTRACT),
        FakeGuidance({"guidance_text": "第二条"}),
    )
    assert start(client, base, headers, aid, "blocked").status_code == 409
    simulate_delivery(db, root, base, aid, second)
    response = start(client, base, headers, aid, "replay-2")
    assert (
        response.status_code == 200
        and response.json()["guidance_audio_id"] != original["guidance_audio_id"]
    )
    assert finish(client, base, headers, aid, response.json(), True).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_count"] == 2 and state["speaking"]["effective_help_count"] == 2
    with Session(db) as session:
        assert session.get(SpeakingHelpRequest, first).status == "DELIVERED"
        assert session.get(SpeakingHelpRequest, second).status == "DELIVERED"


def test_voided_help_and_redo_restore_correct_target(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    first = UUID(help_request(client, base, headers, aid, "first").json()["help_request_id"])
    assert execute(db, claim(db, first, prompt="prompt", contract=CONTRACT), FakeGuidance())
    simulate_delivery(db, root, base, aid, first)
    second = UUID(help_request(client, base, headers, aid, "second").json()["help_request_id"])
    assert execute(db, claim(db, second, prompt="prompt", contract=CONTRACT), FakeGuidance())
    simulate_delivery(db, root, base, aid, second, void=True)
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_target"]["help_request_id"] == str(first)
    response = start(client, base, headers, aid, "old-replay")
    assert response.status_code == 200
    old = response.json()
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": aid},
    )
    assert redo.status_code == 200
    new_id = redo.json()["attempt_id"]
    assert finish(client, base, headers, aid, old, True).status_code == 409
    assert start(client, base, headers, new_id, "premature").status_code == 409
    prompt = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": new_id, "request_id": "initial", "kind": "INITIAL_PROMPT"},
    ).json()
    assert finish(client, base, headers, new_id, prompt, True).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_count"] == state["speaking"]["effective_help_count"] == 0
    assert state["speaking"]["replay_target"]["source"] == "INITIAL_PROMPT"
    new_play = start(client, base, headers, new_id, "new-replay")
    assert new_play.status_code == 200 and new_play.json()["guidance_audio_id"] is None


def test_missing_latest_audio_never_falls_back(db, client, flow):
    from sqlalchemy import select

    from app.api.teacher_api import get_audio_store
    from app.main import app
    from app.task_types.speaking_guidance import GuidanceAudio

    root, base, headers, aid = ready(client, flow)
    help_id = UUID(help_request(client, base, headers, aid).json()["help_request_id"])
    assert execute(db, claim(db, help_id, prompt="prompt", contract=CONTRACT), FakeGuidance())
    simulate_delivery(db, root, base, aid, help_id)
    with Session(db) as session:
        audio = session.scalar(
            select(GuidanceAudio).where(GuidanceAudio.help_request_id == help_id)
        )
        (app.dependency_overrides[get_audio_store]().root / audio.storage_ref).unlink()
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert state["speaking"]["replay_target"]["source"] == "GUIDANCE"
    assert not state["speaking"]["replay_target"]["available"]
    assert "START_USER_REPLAY" not in state["allowed_actions"]
    assert start(client, base, headers, aid, "unavailable").status_code == 409
    assert state["speaking"]["effective_help_count"] == 1


def test_replay_blocked_during_recording_and_pending_asr(db, client, flow):
    from test_speaking_process_asr import REQUIREMENT, FakeASR, segment

    from app.task_types.speaking_asr_worker import claim as claim_asr
    from app.task_types.speaking_asr_worker import execute as execute_asr

    root, base, headers, aid = ready(client, flow)
    recording = client.post(
        base + "/recordings", headers=headers, json={"attempt_id": aid, "request_id": "record"}
    )
    assert recording.status_code == 200
    assert start(client, base, headers, aid, "blocked-record").status_code == 409
    assert (
        "START_USER_REPLAY"
        not in client.get(root, headers=headers).json()["tasks"][0]["allowed_actions"]
    )
    assert (
        client.post(
            base + "/recordings/" + recording.json()["recording_id"] + "/cancel",
            headers=headers,
            json={"attempt_id": aid},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, aid, "saved")
    assert start(client, base, headers, aid, "blocked-asr").status_code == 409
    assert (
        "START_USER_REPLAY"
        not in client.get(root, headers=headers).json()["tasks"][0]["allowed_actions"]
    )
    assert execute_asr(db, claim_asr(db, operation, REQUIREMENT), FakeASR())
    assert start(client, base, headers, aid, "after-asr").status_code == 200


@pytest.mark.parametrize("competitor", ["same", "different", "record", "help", "redo"])
def test_replay_start_concurrency_is_atomic(db, client, flow, competitor):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    barrier = Barrier(2)

    def first():
        barrier.wait(timeout=10)
        return start(client, base, headers, aid, "same")

    def second():
        barrier.wait(timeout=10)
        if competitor in ("same", "different"):
            return start(client, base, headers, aid, "same" if competitor == "same" else "other")
        if competitor == "record":
            return client.post(
                base + "/recordings",
                headers=headers,
                json={"attempt_id": aid, "request_id": "record"},
            )
        if competitor == "help":
            return help_request(client, base, headers, aid, "help")
        return client.post(
            base + "/redo",
            headers=headers,
            json={"redo_request_id": "redo", "expected_attempt_id": aid},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        response, other = a.result(timeout=15), b.result(timeout=15)
    if competitor == "same":
        assert response.status_code == other.status_code == 200
        assert response.json()["playback_id"] == other.json()["playback_id"]
    elif competitor == "redo":
        assert other.status_code == 200 and response.status_code in (200, 409)
        assert client.get(root, headers=headers).json()["tasks"][0]["speaking"]["replay_count"] == 0
    else:
        assert sorted([response.status_code, other.status_code]) == [200, 409]


def test_retry_and_new_replay_compete(db, client, flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, base, headers, aid = ready(client, flow)
    original = start(client, base, headers, aid, "old").json()
    assert finish(client, base, headers, aid, original, False).status_code == 200
    barrier = Barrier(2)

    def recover():
        barrier.wait(timeout=10)
        return retry(client, base, headers, aid, original, "retry")

    def fresh():
        barrier.wait(timeout=10)
        return start(client, base, headers, aid, "new")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(recover), pool.submit(fresh)
        assert sorted([a.result(timeout=15).status_code, b.result(timeout=15).status_code]) == [
            200,
            409,
        ]


def test_retry_keeps_old_audio_after_new_guidance(db, client, flow):
    root, base, headers, aid = ready(client, flow)
    first = UUID(help_request(client, base, headers, aid, "first").json()["help_request_id"])
    assert execute(
        db,
        claim(db, first, prompt="prompt", contract=CONTRACT),
        FakeGuidance({"guidance_text": "第一条"}),
    )
    simulate_delivery(db, root, base, aid, first)
    original = start(client, base, headers, aid, "old-replay").json()
    assert finish(client, base, headers, aid, original, False).status_code == 200
    second = UUID(help_request(client, base, headers, aid, "second").json()["help_request_id"])
    assert execute(
        db,
        claim(db, second, prompt="prompt", contract=CONTRACT),
        FakeGuidance({"guidance_text": "第二条"}),
    )
    simulate_delivery(db, root, base, aid, second)
    retransmit = start(client, base, headers, aid, "old-replay")
    assert (
        retransmit.status_code == 200
        and retransmit.json()["playback_id"] == original["playback_id"]
    )
    resumed = retry(client, base, headers, aid, original, "retry")
    assert (
        resumed.status_code == 200
        and resumed.json()["guidance_audio_id"] == original["guidance_audio_id"]
    )
    assert finish(client, base, headers, aid, resumed.json(), True).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]["speaking"]
    assert state["effective_help_count"] == 2 and state["replay_count"] == 1
    assert state["replay_target"]["help_request_id"] == str(second)


@pytest.mark.parametrize("competitor", ["redo", "failure"])
def test_replay_completion_competition(db, client, flow, competitor):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, base, headers, aid = ready(client, flow)
    original = start(client, base, headers, aid, "replay").json()
    barrier = Barrier(2)

    def success():
        barrier.wait(timeout=10)
        return finish(client, base, headers, aid, original, True)

    def other():
        barrier.wait(timeout=10)
        if competitor == "failure":
            return finish(client, base, headers, aid, original, False)
        return client.post(
            base + "/redo",
            headers=headers,
            json={"redo_request_id": "redo", "expected_attempt_id": aid},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(success), pool.submit(other)
        result, competing = a.result(timeout=15), b.result(timeout=15)
    state = client.get(root, headers=headers).json()["tasks"][0]["speaking"]
    if competitor == "redo":
        assert competing.status_code == 200 and result.status_code in (200, 409)
        assert state["replay_count"] == 0
        assert finish(client, base, headers, aid, original, True).status_code == 409
    else:
        assert sorted([result.status_code, competing.status_code]) == [200, 409]
        assert state["replay_count"] == (1 if result.status_code == 200 else 0)


def test_deleted_frozen_audio_cannot_complete_replay(db, client, flow):
    from app.api.teacher_api import get_audio_store
    from app.main import app
    from app.media.fixed_audio import FixedAudio

    root, base, headers, aid = ready(client, flow)
    playback = start(client, base, headers, aid, "replay").json()
    with Session(db) as session:
        audio = session.get(FixedAudio, UUID(playback["audio_id"]))
        (app.dependency_overrides[get_audio_store]().root / audio.storage_ref).unlink()
    assert finish(client, base, headers, aid, playback, True).status_code == 409
    assert client.get(root, headers=headers).json()["tasks"][0]["speaking"]["replay_count"] == 0
    assert finish(client, base, headers, aid, playback, False).status_code == 200
    state = client.get(root, headers=headers).json()["tasks"][0]
    assert "START_USER_REPLAY" not in state["allowed_actions"]
    assert "RETRY_PLAYBACK" not in state["allowed_actions"]
