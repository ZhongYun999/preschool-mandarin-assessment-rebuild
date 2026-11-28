"""Assessment Session authorization and lifecycle HTTP checks."""

import hashlib
import json
import secrets
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_score_settlement import ScorePlugin
from test_submission import deliver, finish, start, submit
from test_teacher_api import AUTH

from app.api.teacher_api import get_audio_store
from app.api.teacher_review import get_scoring_registry
from app.main import app
from app.media.fixed_audio import AudioStore
from app.scoring.registry import Registry
from app.scoring.settlement import settle_session
from app.sessions.assessment_session import AssessmentSession, Attempt, redo_attempt, start_attempt
from app.sessions.close_manifest import close_session
from app.sessions.credentials import SessionCredential
from app.sessions.submission import Playback, Submission

pytest_plugins = ("test_teacher_api", "test_close_manifest")


class _SecretDict(dict):
    def __repr__(self):
        return "<private test context>"


@pytest.fixture
def setup_api(db, client, two_tasks, tmp_path, monkeypatch):
    plugin = ScorePlugin()
    registry = Registry([plugin])
    store = AudioStore(tmp_path)
    app.dependency_overrides[get_audio_store] = lambda: store
    app.dependency_overrides[get_scoring_registry] = lambda: registry
    review_token = secrets.token_urlsafe(32)
    monkeypatch.setenv(
        "TEACHER_REVIEW_TOKEN_HASHES",
        json.dumps({"teacher-test": hashlib.sha256(review_token.encode()).hexdigest()}),
    )
    with Session(db) as s:
        paper_id = s.get(AssessmentSession, two_tasks[0]["session_id"]).paper_id
    try:
        yield _SecretDict(
            {
                "paper_id": paper_id,
                "registry": registry,
                "plugin": plugin,
                "review_auth": _SecretDict({"Authorization": "Bearer " + review_token}),
            }
        )
    finally:
        app.dependency_overrides.pop(get_audio_store, None)
        app.dependency_overrides.pop(get_scoring_registry, None)


def create_body(paper_id, request="create"):
    return {
        "paper_id": str(paper_id),
        "create_request_id": request,
        "review_policy": {"version": "v1", "default_ratio": 0},
    }


def issue(client, sid):
    response = client.post(f"/teacher/assessment/sessions/{sid}/access-token", headers=AUTH)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()["token"]


def access(token):
    return _SecretDict({"Authorization": "Bearer " + token})


def test_create_idempotency_policy_and_auth(db, client, setup_api):
    p = setup_api["paper_id"]
    body = create_body(p)
    url = "/teacher/assessment/sessions"
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=setup_api["review_auth"]).status_code == 401
    assert client.post(url, json={**body, "scope": "fake"}, headers=AUTH).status_code == 422
    first = client.post(url, json=body, headers=AUTH)
    assert first.status_code == 200
    sid = first.json()["session_id"]
    assert client.post(url, json=body, headers=AUTH).json()["session_id"] == sid
    zero_float = {**body, "review_policy": {"version": "v1", "default_ratio": -0.0}}
    assert client.post(url, json=zero_float, headers=AUTH).json()["session_id"] == sid
    assert (
        client.post(url, json={**body, "paper_id": str(uuid4())}, headers=AUTH).status_code == 409
    )
    assert (
        client.post(
            url, json={**body, "review_policy": {"version": "v1", "default_ratio": 1}}, headers=AUTH
        ).status_code
        == 409
    )
    assert (
        client.post(
            url,
            json={**body, "review_policy": {"version": "v1", "default_ratio": False}},
            headers=AUTH,
        ).status_code
        == 409
    )
    with Session(db) as s:
        assert s.get(AssessmentSession, UUID(sid)).review_policy == body["review_policy"]
        assert s.scalar(select(func.count()).select_from(SessionCredential)) == 0
    for policy in (
        {"sampling": 0},
        {"version": "v2", "default_ratio": 0},
        {"version": "v1", "default_ratio": 2},
        {"version": "v1", "default_ratio": 0, "paper_entry_ratios": {str(uuid4()): 1}},
    ):
        bad = {**create_body(p, str(uuid4())), "review_policy": policy}
        assert client.post(url, json=bad, headers=AUTH).status_code == 422
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 2


def test_token_isolation_additive_reissue_and_no_plaintext(db, client, setup_api):
    p = setup_api["paper_id"]
    sid = client.post("/teacher/assessment/sessions", json=create_body(p), headers=AUTH).json()[
        "session_id"
    ]
    other = client.post(
        "/teacher/assessment/sessions", json=create_body(p, "other"), headers=AUTH
    ).json()["session_id"]
    token = issue(client, sid)
    url = f"/assessment/sessions/{sid}"
    view = client.get(url, headers=access(token))
    assert view.status_code == 200 and view.headers["cache-control"] == "no-store"
    assert view.json()["session_id"] == sid and view.json()["status"] == "IN_PROGRESS"
    assert [t["position"] for t in view.json()["tasks"]] == [1, 2]
    assert all(
        t["attempt_id"] is None and t["allowed_actions"] == ["START_ATTEMPT"]
        for t in view.json()["tasks"]
    )
    assert not view.json()["input_closed"] and not view.json()["final_result_available"]
    assert bool(token not in view.text)
    assert client.get(f"/assessment/sessions/{other}", headers=access(token)).status_code == 401
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/teacher/banks", headers=access(token)).status_code == 401
    assert client.get("/teacher/sessions", headers=access(token)).status_code == 401
    assert (
        client.post(
            f"/teacher/assessment/sessions/{sid}/access-token", headers=access(token)
        ).status_code
        == 401
    )
    task_id = view.json()["tasks"][0]["session_task_id"]
    assert (
        client.post(
            f"/teacher/sessions/{sid}/reviews/{task_id}/decisions", headers=access(token), json={}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/teacher/assessment/sessions/{sid}/access-token",
            headers=AUTH,
            json={"token": "caller"},
        ).status_code
        == 422
    )
    again = issue(client, sid)
    assert bool(token != again)
    assert client.get(url, headers=access(token)).status_code == 200
    assert client.get(url, headers=access(again)).status_code == 200
    assert (
        client.post("/teacher/assessment/sessions", json=create_body(p), headers=AUTH).json()[
            "session_id"
        ]
        == sid
    )
    with Session(db) as s:
        values = list(s.scalars(select(SessionCredential.token_digest)))
        assert len(values) == 2 and all(len(v) == 32 for v in values)
        assert bool(hashlib.sha256(token.encode()).digest() in values)
        assert bool(all(token.encode() not in v for v in values))
    for _ in range(2):
        assert client.get(url, headers=access(token)).status_code == 200
    with Session(db) as s:
        task_ids = [UUID(t["session_task_id"]) for t in view.json()["tasks"]]
        assert (
            s.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.session_task_id.in_(task_ids))
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(Playback)) == 0
        assert s.scalar(select(func.count()).select_from(Submission)) == 0


def test_current_attempt_redo_close_and_completion(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    token = issue(client, sid)
    url = f"/assessment/sessions/{sid}"
    initial = client.get(url, headers=access(token)).json()
    contexts = []
    for row in initial["tasks"]:
        with Session(db) as s, s.begin():
            attempt = start_attempt(
                s, session_id=UUID(sid), session_task_id=UUID(row["session_task_id"])
            )
            contexts.append(
                {
                    "session_id": UUID(sid),
                    "session_task_id": UUID(row["session_task_id"]),
                    "attempt_id": attempt.attempt_id,
                }
            )
    stage = client.get(url, headers=access(token)).json()
    assert "START_GUIDE" in stage["tasks"][0]["allowed_actions"]
    for ctx in contexts:
        deliver(db, ctx)
        with Session(db) as s, s.begin():
            submit(s, ctx)
    before = client.get(url, headers=access(token)).json()
    assert before["allowed_actions"] == ["CLOSE_SESSION"]
    with Session(db) as s, s.begin():
        new = redo_attempt(
            s,
            session_id=UUID(sid),
            session_task_id=contexts[0]["session_task_id"],
            expected_attempt_id=contexts[0]["attempt_id"],
            redo_request_id="redo",
        )
        replacement = {**contexts[0], "attempt_id": new.attempt_id}
    after = client.get(url, headers=access(token)).json()
    assert after["tasks"][0]["attempt_id"] == str(replacement["attempt_id"])
    assert not after["tasks"][0]["submission_exists"] and after["allowed_actions"] == []
    deliver(db, replacement)
    with Session(db) as s, s.begin():
        submit(s, replacement, request="replacement")
        close_session(s, session_id=UUID(sid), close_request_id="close")
    closed = client.get(url, headers=access(token)).json()
    assert closed["status"] == "INPUT_CLOSED" and closed["input_closed"]
    assert not closed["final_result_available"] and all(
        not t["allowed_actions"] for t in closed["tasks"]
    )
    assert setup_api["plugin"].calls == []
    with Session(db) as s, s.begin():
        settle_session(s, session_id=UUID(sid), registry=setup_api["registry"])
    calls = list(setup_api["plugin"].calls)
    completed = client.get(url, headers=access(token)).json()
    assert completed["status"] == "COMPLETED" and completed["final_result_available"]
    assert setup_api["plugin"].calls == calls
    text = json.dumps(completed)
    assert all(
        secret not in text
        for secret in (
            "reference_answer",
            "scoring_instruction",
            "ai_followup_scope",
            "payload",
            "review_policy",
            "scope",
        )
    )


def test_failed_playback_state_actions(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    token = issue(client, sid)
    url = f"/assessment/sessions/{sid}"
    task = client.get(url, headers=access(token)).json()["tasks"][0]
    with Session(db) as s, s.begin():
        attempt = start_attempt(
            s, session_id=UUID(sid), session_task_id=UUID(task["session_task_id"])
        )
        ctx = {
            "session_id": UUID(sid),
            "session_task_id": UUID(task["session_task_id"]),
            "attempt_id": attempt.attempt_id,
        }
        playback = start(s, ctx, "GUIDE", "guide")
        playback_id = playback.playback_id
    busy = client.get(url, headers=access(token)).json()["tasks"][0]["allowed_actions"]
    assert "FINISH_PLAYBACK" in busy and "START_GUIDE" not in busy
    with Session(db) as s, s.begin():
        finish(s, ctx, playback_id, False)
    actions = client.get(url, headers=access(token)).json()["tasks"][0]["allowed_actions"]
    assert "RETRY_PLAYBACK" in actions and "START_GUIDE" in actions


@pytest.mark.db_isolated
def test_downgrade_rejects_existing_credentials(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    issue(client, sid)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(DBAPIError, match="must be preserved"):
            command.downgrade(config, "0015_review_finalization")
        connection.rollback()
        assert connection.scalar(select(func.count()).select_from(SessionCredential)) == 1


def test_unpublished_unready_and_missing_plugin_leave_no_session(db, client, bank, tmp_path):
    from test_fixed_audio import published
    from test_paper import draft

    paper, _, _, _, registry = published(db, bank)
    draft_id = draft(db, bank)
    app.dependency_overrides[get_audio_store] = lambda: AudioStore(tmp_path)
    app.dependency_overrides[get_scoring_registry] = lambda: registry
    try:
        assert (
            client.post(
                "/teacher/assessment/sessions", json=create_body(draft_id, "draft"), headers=AUTH
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/teacher/assessment/sessions", json=create_body(paper, "unready"), headers=AUTH
            ).status_code
            == 409
        )
        app.dependency_overrides[get_scoring_registry] = lambda: Registry()
        assert (
            client.post(
                "/teacher/assessment/sessions", json=create_body(paper, "missing"), headers=AUTH
            ).status_code
            == 503
        )
        with Session(db) as s:
            assert s.scalar(select(func.count()).select_from(AssessmentSession)) == 0
            assert s.scalar(select(func.count()).select_from(SessionCredential)) == 0
    finally:
        app.dependency_overrides.pop(get_audio_store, None)
        app.dependency_overrides.pop(get_scoring_registry, None)


def test_historical_policy_idempotent_retry_without_new_registry(db, client, setup_api, tmp_path):
    from app.sessions.assessment_session import create_session

    paper_id = setup_api["paper_id"]
    with Session(db) as s, s.begin():
        sid = create_session(
            s,
            paper_id=paper_id,
            scope="teacher-management",
            create_request_id="historical",
            review_policy={"sampling": 0},
            store=AudioStore(tmp_path),
            registry=setup_api["registry"],
        ).session_id
    app.dependency_overrides[get_scoring_registry] = lambda: Registry()
    body = {
        "paper_id": str(paper_id),
        "create_request_id": "historical",
        "review_policy": {"sampling": 0},
    }
    response = client.post("/teacher/assessment/sessions", json=body, headers=AUTH)
    assert response.status_code == 200 and response.json()["session_id"] == str(sid)
    assert (
        client.post(
            "/teacher/assessment/sessions",
            json={**body, "review_policy": {"sampling": False}},
            headers=AUTH,
        ).status_code
        == 409
    )
    with Session(db) as s:
        assert s.get(AssessmentSession, sid).review_policy == {"sampling": 0}
        assert s.scalar(select(func.count()).select_from(SessionCredential)) == 0


def test_token_transaction_failure_and_auth_failure_are_safe(db, client, setup_api):
    from sqlalchemy import event

    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    endpoint = f"/teacher/assessment/sessions/{sid}/access-token"
    assert client.post(endpoint, headers=setup_api["review_auth"]).status_code == 401

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SESSION_CREDENTIALS"):
            raise RuntimeError("credential insert failure")

    event.listen(db, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="credential insert failure"):
            client.post(endpoint, headers=AUTH)
    finally:
        event.remove(db, "before_cursor_execute", fail)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(SessionCredential)) == 0
    assert (
        client.get(
            f"/assessment/sessions/{uuid4()}", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 401
    )


@pytest.mark.db_isolated
def test_existing_sessions_survive_credential_migration(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0015_review_finalization")
        connection.commit()
        command.upgrade(config, "head")
        connection.commit()
    with Session(db) as s:
        assert s.get(AssessmentSession, UUID(sid)).status == "IN_PROGRESS"
    token = issue(client, sid)
    assert client.get(f"/assessment/sessions/{sid}", headers=access(token)).status_code == 200


def test_guide_retry_recovery_and_prompt_readiness(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", json=create_body(setup_api["paper_id"]), headers=AUTH
    ).json()["session_id"]
    token = issue(client, sid)
    url = f"/assessment/sessions/{sid}"
    task = client.get(url, headers=access(token)).json()["tasks"][0]
    with Session(db) as s, s.begin():
        attempt = start_attempt(
            s, session_id=UUID(sid), session_task_id=UUID(task["session_task_id"])
        )
        ctx = {
            "session_id": UUID(sid),
            "session_task_id": UUID(task["session_task_id"]),
            "attempt_id": attempt.attempt_id,
        }
        original = start(s, ctx, "GUIDE", "guide")
        original_id = original.playback_id
        finish(s, ctx, original_id, False)
        retry = start(s, ctx, "TECHNICAL_RETRY", "retry", original_id)
        finish(s, ctx, retry.playback_id)
    actions = client.get(url, headers=access(token)).json()["tasks"][0]["allowed_actions"]
    assert "START_INITIAL_PROMPT" in actions and "START_GUIDE" not in actions
    with Session(db) as s, s.begin():
        prompt = start(s, ctx, "INITIAL_PROMPT", "prompt")
        finish(s, ctx, prompt.playback_id)
    actions = client.get(url, headers=access(token)).json()["tasks"][0]["allowed_actions"]
    assert "SUBMIT_INSTRUCTION" in actions and "START_USER_REPLAY" in actions


def test_empty_paper_keeps_legal_close_action(db, client, bank, tmp_path):
    from test_paper import draft
    from test_publication import TEST_AUDIO_CONFIG

    from app.papers.publication import publish, select_plugin

    plugin = ScorePlugin()
    registry = Registry([plugin])
    paper_id = draft(db, bank)
    with Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=paper_id,
            expected_version=0,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        publish(
            s,
            paper_id=paper_id,
            expected_version=1,
            registry=registry,
            audio_config=TEST_AUDIO_CONFIG,
        )
    app.dependency_overrides[get_audio_store] = lambda: AudioStore(tmp_path)
    app.dependency_overrides[get_scoring_registry] = lambda: registry
    try:
        sid = client.post(
            "/teacher/assessment/sessions", json=create_body(paper_id), headers=AUTH
        ).json()["session_id"]
        token = issue(client, sid)
        state = client.get(f"/assessment/sessions/{sid}", headers=access(token)).json()
        assert state["tasks"] == [] and state["allowed_actions"] == ["CLOSE_SESSION"]
    finally:
        app.dependency_overrides.pop(get_audio_store, None)
        app.dependency_overrides.pop(get_scoring_registry, None)
