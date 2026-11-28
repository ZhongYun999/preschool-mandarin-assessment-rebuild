"""HTTP input flow and redo/playback transaction boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy.orm import Session
from test_assessment_http import access, create_body, issue
from test_submission import deliver, finish, start, submit
from test_teacher_api import AUTH

from app.sessions.assessment_session import Attempt, SessionTask, redo_attempt
from app.sessions.close_manifest import IncompleteSession, close_session
from app.sessions.submission import Playback, Submission

pytest_plugins = ("test_assessment_http",)


def test_redo_request_identity_stale_and_recovery(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(setup_api["paper_id"])
    ).json()["session_id"]
    headers = access(issue(client, sid))
    root = f"/assessment/sessions/{sid}"
    tasks = client.get(root, headers=headers).json()["tasks"]
    base = f"{root}/tasks/{tasks[0]['session_task_id']}"
    url = base + "/redo"
    assert client.post(url, headers=headers, json={"redo_request_id": "missing"}).status_code == 422
    assert (
        client.post(
            url, headers=headers, json={"redo_request_id": "bad", "expected_attempt_id": "invalid"}
        ).status_code
        == 422
    )
    initial = {"redo_request_id": "initial", "expected_attempt_id": None}
    a = client.post(url, headers=headers, json=initial).json()["attempt_id"]
    assert (
        client.post(
            url,
            headers=headers,
            json={"redo_request_id": "stale-null", "expected_attempt_id": None},
        ).status_code
        == 409
    )
    r1 = {"redo_request_id": "r1", "expected_attempt_id": a}
    b = client.post(url, headers=headers, json=r1).json()["attempt_id"]
    assert client.post(url, headers=headers, json=r1).json()["attempt_id"] == b
    stale = client.post(
        url, headers=headers, json={"redo_request_id": "r2", "expected_attempt_id": a}
    )
    assert stale.status_code == 409 and "no longer current" in stale.text
    state = client.get(root, headers=headers)
    assert state.status_code == 200 and state.json()["tasks"][0]["attempt_id"] == b
    c = client.post(
        url, headers=headers, json={"redo_request_id": "r3", "expected_attempt_id": b}
    ).json()["attempt_id"]
    historic = client.post(url, headers=headers, json=r1).json()
    assert historic == {"attempt_id": b, "status": "ABANDONED", "current_attempt_id": c}
    assert (
        client.post(
            url, headers=headers, json={"redo_request_id": "r1", "expected_attempt_id": c}
        ).json()
        == historic
    )
    assert client.get(root, headers=headers).json()["tasks"][0]["attempt_id"] == c
    assert client.post(f"{root}/tasks/{uuid4()}/redo", headers=headers, json=r1).status_code == 404
    assert client.post(url, headers={}, json=r1).status_code == 401
    with Session(db) as s:
        assert s.get(SessionTask, UUID(tasks[0]["session_task_id"])).current_attempt_id == UUID(c)


def test_http_flow_and_redo(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(setup_api["paper_id"])
    ).json()["session_id"]
    token = issue(client, sid)
    headers = access(token)
    root = f"/assessment/sessions/{sid}"
    tasks = client.get(root, headers=headers).json()["tasks"]
    base = f"{root}/tasks/{tasks[0]['session_task_id']}"
    assert client.get(base + "/content", headers=AUTH).status_code == 401
    assert client.post(base + "/attempt", headers=setup_api["review_auth"]).status_code == 401
    assert client.get(f"{root}/tasks/{uuid4()}/content", headers=headers).status_code == 404
    content = client.get(base + "/content", headers=headers)
    assert content.status_code == 200
    assert all(
        x not in content.text
        for x in ("reference_answer", "scoring_instruction", "ai_followup_scope")
    )
    assert [c["choice_id"] for c in content.json()["choices"]] == ["A", "B", "C"]
    assert client.get(content.json()["audio"]["prompt"], headers=headers).status_code == 200
    assert client.get(content.json()["audio"]["prompt"], headers=AUTH).status_code == 401
    assert client.get(base + "/attempt", headers=headers).json()["attempt"] is None
    first = client.post(base + "/attempt", headers=headers).json()["attempt_id"]
    assert client.post(base + "/attempt", headers=headers).json()["attempt_id"] == first

    def play(attempt, kind, request):
        return client.post(
            base + "/playbacks",
            headers=headers,
            json={"attempt_id": attempt, "kind": kind, "request_id": request},
        )

    guide = play(first, "GUIDE", "g").json()
    assert play(first, "GUIDE", "g").json()["playback_id"] == guide["playback_id"]
    assert play(first, "INITIAL_PROMPT", "p").status_code == 409
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": first},
    ).json()
    second = redo["attempt_id"]
    assert second != first
    assert (
        client.post(
            base + "/redo",
            headers=headers,
            json={"redo_request_id": "redo", "expected_attempt_id": first},
        ).json()
        == redo
    )
    finish_url = base + "/playbacks/" + guide["playback_id"] + "/finish"
    assert (
        client.post(
            finish_url, headers=headers, json={"attempt_id": first, "completed": True}
        ).status_code
        == 409
    )

    def finish_http(row, attempt, done=True):
        return client.post(
            base + "/playbacks/" + row["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": attempt, "completed": done},
        )

    for kind, request in [("GUIDE", "g"), ("INITIAL_PROMPT", "p"), ("USER_REPLAY", "r")]:
        row = play(second, kind, request).json()
        assert finish_http(row, second).status_code == 200
        assert finish_http(row, second).status_code == 200
    body = {"attempt_id": second, "submit_request_id": "submit", "selected_choice_id": "B"}
    result = client.post(base + "/submissions/instruction", headers=headers, json=body)
    assert result.status_code == 200
    assert (
        client.post(base + "/submissions/instruction", headers=headers, json=body).json()
        == result.json()
    )
    assert (
        client.post(
            base + "/submissions/instruction",
            headers=headers,
            json={**body, "selected_choice_id": "C"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + "/submissions/instruction",
            headers=headers,
            json={**body, "selected_choice_id": "D"},
        ).status_code
        == 422
    )
    with Session(db) as s:
        submission = s.get(Submission, UUID(result.json()["submission_id"]))
        assert len(submission.inputs["replay_playback_ids"]) == 1
        assert s.get(Attempt, UUID(first)).status == "ABANDONED"
        assert s.get(Playback, UUID(guide["playback_id"])).status == "STARTED"
    incomplete = client.post(root + "/close", headers=headers, json={"close_request_id": "close"})
    assert incomplete.status_code == 409
    assert tasks[1]["session_task_id"] in incomplete.json()["detail"]["missing_session_task_ids"]
    assert setup_api["plugin"].calls == []


def test_finish_and_redo_race(db, context):
    with Session(db) as s, s.begin():
        old_playback = start(s, context, "GUIDE", "g").playback_id
    barrier = Barrier(2)

    def do_finish():
        barrier.wait()
        try:
            with Session(db) as s, s.begin():
                finish(s, context, old_playback)
            return "completed"
        except ValueError:
            return "rejected"

    def do_redo():
        barrier.wait()
        with Session(db) as s, s.begin():
            return redo_attempt(
                s,
                session_id=context["session_id"],
                session_task_id=context["session_task_id"],
                expected_attempt_id=context["attempt_id"],
                redo_request_id="redo",
            ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(do_finish), pool.submit(do_redo)
        assert a.result() in ("completed", "rejected")
        new_id = b.result()
    with Session(db) as s, s.begin():
        assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == new_id
        assert s.get(Attempt, context["attempt_id"]).status == "ABANDONED"
        assert (
            redo_attempt(
                s,
                session_id=context["session_id"],
                session_task_id=context["session_task_id"],
                expected_attempt_id=context["attempt_id"],
                redo_request_id="redo",
            ).attempt_id
            == new_id
        )
        new_context = {**context, "attempt_id": new_id}
        assert start(s, new_context, "GUIDE", "g").attempt_id == new_id
    with Session(db) as s:
        assert s.get(Playback, old_playback).status in ("STARTED", "COMPLETED")


def test_old_submission_never_restored_after_redo(db, context):
    deliver(db, context)
    with Session(db) as s, s.begin():
        old = submit(s, context).submission_id
        new = redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            expected_attempt_id=context["attempt_id"],
            redo_request_id="redo",
        ).attempt_id
    try:
        with Session(db) as s, s.begin():
            close_session(s, session_id=context["session_id"], close_request_id="close")
        assert False, "unsubmitted redo must prevent close"
    except IncompleteSession:
        pass
    with Session(db) as s:
        assert s.get(SessionTask, context["session_task_id"]).current_attempt_id == new
        assert s.get(Submission, old) is not None


def test_http_close_and_session_isolation(db, client, setup_api):
    paper = setup_api["paper_id"]
    sid = client.post("/teacher/assessment/sessions", headers=AUTH, json=create_body(paper)).json()[
        "session_id"
    ]
    other = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(paper, "other")
    ).json()["session_id"]
    headers = access(issue(client, sid))
    other_headers = access(issue(client, other))
    root = f"/assessment/sessions/{sid}"
    tasks = client.get(root, headers=headers).json()["tasks"]
    assert client.get(root, headers=other_headers).status_code == 401
    first_base = f"{root}/tasks/{tasks[0]['session_task_id']}"
    assert client.post(first_base + "/attempt", headers=other_headers).status_code == 401
    assert client.get(first_base + "/content", headers=other_headers).status_code == 401
    other_task = client.get(f"/assessment/sessions/{other}", headers=other_headers).json()["tasks"][
        0
    ]["session_task_id"]
    assert client.get(f"{root}/tasks/{other_task}/content", headers=headers).status_code == 404
    redo_body = {"redo_request_id": "cross-session", "expected_attempt_id": None}
    assert (
        client.post(first_base + "/redo", headers=other_headers, json=redo_body).status_code == 401
    )
    assert (
        client.post(f"{root}/tasks/{other_task}/redo", headers=headers, json=redo_body).status_code
        == 404
    )
    first_audio = client.get(first_base + "/content", headers=headers).json()["audio"]["prompt"]
    assert client.get(first_audio, headers=other_headers).status_code == 401
    for task, choice in zip(tasks, ("A", "C"), strict=True):
        base = f"{root}/tasks/{task['session_task_id']}"
        attempt = client.post(base + "/attempt", headers=headers).json()["attempt_id"]
        for kind in ("GUIDE", "INITIAL_PROMPT"):
            playback = client.post(
                base + "/playbacks",
                headers=headers,
                json={"attempt_id": attempt, "kind": kind, "request_id": kind},
            ).json()
            assert (
                client.post(
                    base + "/playbacks/" + playback["playback_id"] + "/finish",
                    headers=headers,
                    json={"attempt_id": attempt, "completed": True},
                ).status_code
                == 200
            )
        body = {"attempt_id": attempt, "submit_request_id": "submit", "selected_choice_id": choice}
        assert (
            client.post(base + "/submissions/instruction", headers=headers, json=body).status_code
            == 200
        )
    result = client.post(root + "/close", headers=headers, json={"close_request_id": "close"})
    assert result.status_code == 200 and result.json()["status"] == "INPUT_CLOSED"
    assert (
        client.post(root + "/close", headers=headers, json={"close_request_id": "close"}).json()
        == result.json()
    )
    assert (
        client.post(
            root + "/close", headers=headers, json={"close_request_id": "other"}
        ).status_code
        == 409
    )
    assert client.post(first_base + "/attempt", headers=headers).status_code == 409
    assert (
        client.post(
            first_base + "/redo",
            headers=headers,
            json={"redo_request_id": "later", "expected_attempt_id": None},
        ).status_code
        == 409
    )
    assert setup_api["plugin"].calls == []


def test_frozen_image_and_no_guide(db, bank, client, tmp_path):
    from dataclasses import replace
    from hashlib import sha256
    from uuid import uuid4

    from test_assessment_session import create
    from test_fixed_audio import Synth
    from test_paper import draft
    from test_publication import TEST_AUDIO_CONFIG, TestPlugin
    from test_taskbank import CONTENT, write

    from app.api.teacher_api import get_audio_store
    from app.main import app
    from app.media.fixed_audio import AudioStore, prepare_audio
    from app.papers.paper import add_entry
    from app.papers.publication import publish, select_plugin
    from app.scoring.registry import Registry
    from app.sessions.assessment_session import ordered_tasks
    from app.taskbank.models import Asset

    data = b"test-image-bytes"
    store = AudioStore(tmp_path)
    ref, _digest = store.put(data)
    asset_id = uuid4()
    with Session(db) as s, s.begin():
        s.add(
            Asset(
                asset_id=asset_id,
                sha256=sha256(data).hexdigest(),
                storage_ref=ref,
                media_type="image/png",
            )
        )
    old = write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", guide_text=None, reference_answer="A"),
        images={"option_A": asset_id, "question_image": asset_id},
    )
    plugin = TestPlugin()
    plugin.evaluation_requirements["instruction"] = ()
    registry = Registry([plugin], [])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        entry = add_entry(
            s, paper_id=paper, expected_version=0, task_id=old.task_id, position=1
        ).paper_entry_id
        select_plugin(
            s,
            paper_id=paper,
            expected_version=1,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        publish(
            s, paper_id=paper, expected_version=2, registry=registry, audio_config=TEST_AUDIO_CONFIG
        )
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        sid = create(s, (paper, old, entry, None, registry, store)).session_id
        task = ordered_tasks(s, sid)[0].session_task_id
    write(
        db,
        bank,
        replace(CONTENT, task_type="instruction", prompt_text="new revision", reference_answer="B"),
    )
    app.dependency_overrides[get_audio_store] = lambda: store
    headers = access(issue(client, sid))
    base = f"/assessment/sessions/{sid}/tasks/{task}"
    content = client.get(base + "/content", headers=headers).json()
    assert content["prompt_text"] == CONTENT.prompt_text and not content["has_guide"]
    assert client.get(content["question_image_url"], headers=headers).content == data
    assert client.get(content["choices"][0]["image_url"], headers=headers).content == data
    assert client.get(base + "/images/B", headers=headers).status_code == 404
    attempt = client.post(base + "/attempt", headers=headers).json()["attempt_id"]
    row = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": attempt, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    ).json()
    finish_url = base + "/playbacks/" + row["playback_id"] + "/finish"
    assert (
        client.post(
            finish_url, headers=headers, json={"attempt_id": attempt, "completed": False}
        ).status_code
        == 200
    )
    retry = client.post(
        base + "/playbacks",
        headers=headers,
        json={
            "attempt_id": attempt,
            "request_id": "retry",
            "kind": "TECHNICAL_RETRY",
            "retry_of_id": row["playback_id"],
        },
    ).json()
    assert (
        client.post(
            base + "/playbacks/" + retry["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": attempt, "completed": True},
        ).status_code
        == 200
    )
    submitted = client.post(
        base + "/submissions/instruction",
        headers=headers,
        json={"attempt_id": attempt, "submit_request_id": "s", "selected_choice_id": "A"},
    )
    assert submitted.status_code == 200
    with Session(db) as s:
        assert (
            s.get(Submission, UUID(submitted.json()["submission_id"])).inputs["replay_playback_ids"]
            == []
        )
    (tmp_path / ref).write_bytes(b"corrupt")
    assert client.get(content["question_image_url"], headers=headers).status_code == 404
