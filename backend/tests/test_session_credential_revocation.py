"""Revocation, credential migration, and HTTP transaction serialization."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_assessment_http import access, create_body
from test_submission import deliver, submit
from test_teacher_api import AUTH

from app.scoring.models import FinalScoreResult
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.sessions.close_manifest import CloseManifest
from app.sessions.credentials import SessionCredential
from app.sessions.submission import Submission

pytest_plugins = ("test_assessment_http", "test_score_review")


def issue_full(client, sid):
    response = client.post(f"/teacher/assessment/sessions/{sid}/access-token", headers=AUTH)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()


def test_listing_revocation_recovery_and_access_boundaries(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(setup_api["paper_id"])
    ).json()["session_id"]
    other = client.post(
        "/teacher/assessment/sessions",
        headers=AUTH,
        json=create_body(setup_api["paper_id"], "other"),
    ).json()["session_id"]
    first, second, outsider = (
        issue_full(client, sid),
        issue_full(client, sid),
        issue_full(client, other),
    )
    h1, h2, h_other = access(first["token"]), access(second["token"]), access(outsider["token"])
    root = f"/assessment/sessions/{sid}"
    admin = f"/teacher/assessment/sessions/{sid}/access-tokens"
    state = client.get(root, headers=h1).json()
    task = state["tasks"][0]["session_task_id"]
    base = f"{root}/tasks/{task}"
    audio = client.get(base + "/content", headers=h1).json()["audio"]["prompt"]
    listing = client.get(admin, headers=AUTH)
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert {x["credential_id"] for x in listing.json()} == {
        first["credential_id"],
        second["credential_id"],
    }
    assert [(x["issued_at"], x["credential_id"]) for x in listing.json()] == sorted(
        (x["issued_at"], x["credential_id"]) for x in listing.json()
    )
    assert all(
        set(x) == {"credential_id", "issued_at", "revoked_at", "active"} and x["active"]
        for x in listing.json()
    )
    assert first["token"] not in listing.text and second["token"] not in listing.text
    assert client.get(admin, headers=h1).status_code == 401
    assert client.get(admin, headers=setup_api["review_auth"]).status_code == 401
    assert (
        client.post(admin + "/" + outsider["credential_id"] + "/revoke", headers=AUTH).status_code
        == 404
    )
    revoke_url = admin + "/" + first["credential_id"] + "/revoke"
    revoked = client.post(revoke_url, headers=AUTH)
    assert (
        revoked.status_code == 200 and not revoked.json()["active"] and revoked.json()["revoked_at"]
    )
    assert client.post(revoke_url, headers=AUTH).json() == revoked.json()
    urls = [root, base + "/content", audio, base + "/images/question", base + "/attempt"]
    assert all(client.get(url, headers=h1).status_code == 401 for url in urls)
    bad = {"attempt_id": str(uuid4()), "submit_request_id": "x", "selected_choice_id": "A"}
    operations = [
        ("/attempt", None),
        ("/redo", {"redo_request_id": "r"}),
        ("/submissions/instruction", bad),
        ("/playbacks", {"attempt_id": str(uuid4()), "request_id": "x", "kind": "GUIDE"}),
        ("/playbacks/" + str(uuid4()) + "/finish", {"attempt_id": str(uuid4()), "completed": True}),
    ]
    for path, body in operations:
        assert client.post(base + path, headers=h1, json=body).status_code == 401
    assert (
        client.post(root + "/close", headers=h1, json={"close_request_id": "c"}).status_code == 401
    )
    assert client.get(root, headers=h2).status_code == 200
    started = client.post(base + "/attempt", headers=h2)
    assert started.status_code == 200
    assert client.get(f"/assessment/sessions/{other}", headers=h_other).status_code == 200
    assert (
        client.get(root, headers=h1).json()
        == client.get(root, headers={"Authorization": "Bearer invalid"}).json()
    )
    assert client.post(admin + "/revoke-all", headers=AUTH).json()["revoked_count"] == 1
    assert client.post(admin + "/revoke-all", headers=AUTH).json()["revoked_count"] == 0
    assert client.get(root, headers=h2).status_code == 401
    restored = issue_full(client, sid)
    restored_state = client.get(root, headers=access(restored["token"]))
    assert restored_state.status_code == 200
    assert restored_state.json()["tasks"][0]["attempt_id"] == started.json()["attempt_id"]
    assert client.get(f"/assessment/sessions/{other}", headers=h_other).status_code == 200
    with Session(db) as s:
        rows = list(
            s.scalars(select(SessionCredential).where(SessionCredential.session_id == UUID(sid)))
        )
        assert len(rows) == 3 and sum(x.revoked_at is None for x in rows) == 1
        assert s.get(AssessmentSession, UUID(sid)).status == "IN_PROGRESS"
        assert (
            s.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.session_task_id == UUID(task))
            )
            == 1
        )


def test_migration_existing_credentials_and_guard(db, client, setup_api):
    sid = client.post(
        "/teacher/assessment/sessions", headers=AUTH, json=create_body(setup_api["paper_id"])
    ).json()["session_id"]
    credential = issue_full(client, sid)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0016_session_credentials")
        connection.commit()
        assert connection.scalar(text("SELECT count(*) FROM session_credentials")) == 1
        connection.commit()
        command.upgrade(config, "head")
        connection.commit()
    assert (
        client.get(f"/assessment/sessions/{sid}", headers=access(credential["token"])).status_code
        == 200
    )
    admin = f"/teacher/assessment/sessions/{sid}/access-tokens/{credential['credential_id']}/revoke"
    assert client.post(admin, headers=AUTH).status_code == 200
    with db.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(DBAPIError, match="revocations must be preserved"):
            command.downgrade(config, "0016_session_credentials")
        connection.rollback()
        assert (
            connection.scalar(text("SELECT revoked_at IS NOT NULL FROM session_credentials"))
            is True
        )
    assert (
        client.get(f"/assessment/sessions/{sid}", headers=access(credential["token"])).status_code
        == 401
    )


@pytest.mark.parametrize("operation", ["attempt", "submission", "redo", "close"])
@pytest.mark.parametrize("first", ["revoke", "write"])
def test_revoke_and_writes_serialized(
    db, client, two_tasks, setup_api, monkeypatch, operation, first
):
    import app.api.assessment_operations as api

    one, two = two_tasks
    if operation == "attempt":
        response = client.post(
            "/teacher/assessment/sessions", headers=AUTH, json=create_body(setup_api["paper_id"])
        )
        assert response.status_code == 200
        sid = UUID(response.json()["session_id"])
        with Session(db) as s:
            task_id = s.scalar(
                select(SessionTask.session_task_id).where(SessionTask.session_id == sid).limit(1)
            )
        one = {"session_id": sid, "session_task_id": task_id}
    if operation in ("submission", "redo", "close"):
        for ctx in (one, two):
            deliver(db, ctx)
    if operation in ("redo", "close"):
        with Session(db) as s, s.begin():
            submit(s, one)
            submit(s, two)
    sid = str(one["session_id"])
    cred = issue_full(client, sid)
    headers = access(cred["token"])
    base = f"/assessment/sessions/{sid}/tasks/{one['session_task_id']}"
    revoke_url = f"/teacher/assessment/sessions/{sid}/access-tokens/{cred['credential_id']}/revoke"
    entered, release, competing = Event(), Event(), Event()
    original = api.confirm_write_access

    def held(db_session, session_id, credential_id):
        if first == "write":
            original(db_session, session_id, credential_id)
        entered.set()
        assert release.wait(10)
        if first == "revoke":
            original(db_session, session_id, credential_id)

    monkeypatch.setattr(api, "confirm_write_access", held)

    def write():
        if operation == "attempt":
            return client.post(base + "/attempt", headers=headers)
        if operation == "submission":
            return client.post(
                base + "/submissions/instruction",
                headers=headers,
                json={
                    "attempt_id": str(one["attempt_id"]),
                    "submit_request_id": "submit",
                    "selected_choice_id": "A",
                },
            )
        if operation == "redo":
            return client.post(base + "/redo", headers=headers, json={"redo_request_id": "redo"})
        return client.post(
            f"/assessment/sessions/{sid}/close", headers=headers, json={"close_request_id": "close"}
        )

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "assessment_sessions" in statement and "FOR UPDATE" in statement:
            competing.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_write = pool.submit(write)
        assert entered.wait(10)
        try:
            if first == "write":
                event.listen(db, "before_cursor_execute", observe)
                future_revoke = pool.submit(client.post, revoke_url, headers=AUTH)
                assert competing.wait(10)
                release.set()
                assert future_write.result(timeout=10).status_code == 200
                assert future_revoke.result(timeout=10).status_code == 200
            else:
                assert client.post(revoke_url, headers=AUTH).status_code == 200
                release.set()
                assert future_write.result(timeout=10).status_code == 401
        finally:
            release.set()
            if first == "write":
                event.remove(db, "before_cursor_execute", observe)
    with Session(db) as s:
        task = s.get(SessionTask, one["session_task_id"])
        if operation == "attempt":
            assert (task.current_attempt_id is not None) == (first == "write")
        if operation == "redo":
            assert (task.current_attempt_id != one["attempt_id"]) == (first == "write")
        if operation == "submission":
            assert (
                s.scalar(select(Submission).where(Submission.attempt_id == one["attempt_id"]))
                is not None
            ) == (first == "write")
        if operation == "close":
            assert (
                s.scalar(select(CloseManifest).where(CloseManifest.session_id == one["session_id"]))
                is not None
            ) == (first == "write")
        assert s.get(SessionCredential, UUID(cred["credential_id"])).revoked_at is not None


@pytest.mark.parametrize("first", ["issue", "revoke"])
def test_issue_and_revoke_all_are_serialized(db, client, two_tasks, monkeypatch, first):
    import app.api.teacher_assessment as api

    sid = str(two_tasks[0]["session_id"])
    old = issue_full(client, sid)
    admin = f"/teacher/assessment/sessions/{sid}"
    entered, release, competing = Event(), Event(), Event()
    if first == "issue":
        original = api.secrets.token_urlsafe

        def hold_issue(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)

        monkeypatch.setattr(api.secrets, "token_urlsafe", hold_issue)
    else:
        original = api._lock_owner

        def hold_revoke(db_session, session_id):
            original(db_session, session_id)
            entered.set()
            assert release.wait(10)

        monkeypatch.setattr(api, "_lock_owner", hold_revoke)

    def issue_request():
        return client.post(admin + "/access-token", headers=AUTH)

    def revoke_request():
        return client.post(admin + "/access-tokens/revoke-all", headers=AUTH)

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "assessment_sessions" in statement and "FOR UPDATE" in statement:
            competing.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(issue_request if first == "issue" else revoke_request)
        assert entered.wait(10)
        event.listen(db, "before_cursor_execute", observe)
        try:
            secondary = pool.submit(revoke_request if first == "issue" else issue_request)
            assert competing.wait(10)
            release.set()
            a, b = primary.result(timeout=10), secondary.result(timeout=10)
        finally:
            release.set()
            event.remove(db, "before_cursor_execute", observe)
    issued, revoked = (a, b) if first == "issue" else (b, a)
    assert issued.status_code == revoked.status_code == 200
    token = issued.json()["token"]
    assert revoked.json()["revoked_count"] == (2 if first == "issue" else 1)
    assert (
        client.get(f"/assessment/sessions/{sid}", headers=access(old["token"])).status_code == 401
    )
    assert client.get(f"/assessment/sessions/{sid}", headers=access(token)).status_code == (
        401 if first == "issue" else 200
    )


def test_revocation_preserves_closed_and_completed_results(db, client, ready_review):
    from app.evaluation.models import RawEvaluation
    from app.scoring.review import record_review
    from app.scoring.settlement import settle_session

    c = ready_review
    sid = str(c["sid"])
    cred = issue_full(client, sid)
    with Session(db) as s:
        original_raw = {x.evaluation_id: x.payload for x in s.scalars(select(RawEvaluation))}
        original_submissions = set(s.scalars(select(Submission.submission_id)))
        manifest = s.get(CloseManifest, c["manifest"])
        assert manifest and s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"
    admin = f"/teacher/assessment/sessions/{sid}/access-tokens"
    assert client.post(admin + "/revoke-all", headers=AUTH).json()["revoked_count"] == 1
    assert (
        client.get(f"/assessment/sessions/{sid}", headers=access(cred["token"])).status_code == 401
    )
    with Session(db) as s, s.begin():
        record_review(
            s,
            session_id=c["sid"],
            session_task_id=c["contexts"][0]["session_task_id"],
            submission_id=c["submissions"][0],
            operation_id="review",
            teacher_id="test",
            expected_revision=0,
            action="CONFIRM",
            registry=c["registry"],
        )
        result_id = settle_session(s, session_id=c["sid"], registry=c["registry"]).result_id
    new = issue_full(client, sid)
    assert (
        client.post(admin + "/" + new["credential_id"] + "/revoke", headers=AUTH).status_code == 200
    )
    with Session(db) as s:
        assert s.get(AssessmentSession, c["sid"]).status == "COMPLETED"
        assert s.get(FinalScoreResult, result_id) is not None
        assert {
            x.evaluation_id: x.payload for x in s.scalars(select(RawEvaluation))
        } == original_raw
        assert set(s.scalars(select(Submission.submission_id))) == original_submissions
        assert s.get(CloseManifest, c["manifest"]) is not None
    assert client.get(f"/teacher/sessions/{sid}/result", headers=AUTH).json()["ready"] is True
