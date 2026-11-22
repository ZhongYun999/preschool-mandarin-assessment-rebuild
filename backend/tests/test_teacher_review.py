"""Teacher review HTTP boundary and credential separation on PostgreSQL."""

import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_teacher_api import AUTH

from app.api.teacher_review import get_scoring_registry
from app.main import app
from app.scoring.models import FinalScoreResult, ReviewDecision, ReviewedPaperScore
from app.sessions.assessment_session import AssessmentSession

pytest_plugins = ("test_teacher_api", "test_score_review")


class _TestCredentials(dict):
    def __repr__(self):
        return "<ephemeral test credentials>"


@pytest.fixture
def identities(client, ready_review, monkeypatch):
    first, second = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    monkeypatch.setenv(
        "TEACHER_REVIEW_TOKEN_HASHES",
        json.dumps({"teacher-a": digest(first), "teacher-b": digest(second)}),
    )
    app.dependency_overrides[get_scoring_registry] = lambda: ready_review["registry"]
    try:
        yield _TestCredentials(
            {
                "a": {"Authorization": "Bearer " + first},
                "b": {"Authorization": "Bearer " + second},
            }
        )
    finally:
        app.dependency_overrides.pop(get_scoring_registry, None)


def path(c, index=0):
    return (
        f"/teacher/sessions/{c['sid']}/reviews/{c['contexts'][index]['session_task_id']}/decisions"
    )


def body(c, index=0, operation="request-1", revision=0, action="CONFIRM", value=None):
    return {
        "submission_id": str(c["submissions"][index]),
        "operation_id": operation,
        "expected_revision": revision,
        "action": action,
        "override_value": value,
    }


def test_credentials_and_separate_permissions(client, ready_review, identities, monkeypatch):
    c = ready_review
    assert client.get("/teacher/banks", headers=AUTH).status_code == 200
    assert client.get("/teacher/sessions", headers=AUTH).status_code == 200
    assert client.get("/teacher/sessions", headers=identities["a"]).status_code == 200
    assert (
        client.get(f"/teacher/sessions/{c['sid']}/reviews", headers=identities["b"]).status_code
        == 200
    )
    assert (
        client.post("/teacher/banks", json={"name": "demo"}, headers=identities["a"]).status_code
        == 401
    )
    assert client.post(path(c), json=body(c), headers=AUTH).status_code == 403
    assert client.post(path(c), json=body(c)).status_code == 401
    assert (
        client.post(path(c), json=body(c), headers={"Authorization": "Bearer invalid"}).status_code
        == 401
    )
    assert (
        client.post(
            path(c), json={**body(c), "teacher_id": "forged"}, headers=identities["a"]
        ).status_code
        == 422
    )
    monkeypatch.delenv("TEACHER_REVIEW_TOKEN_HASHES")
    assert client.post(path(c), json=body(c), headers=identities["a"]).status_code == 503
    assert client.get("/teacher/banks", headers=AUTH).status_code == 200
    assert client.get("/teacher/sessions", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "{}",
        "[]",
        '{"a":"x"}',
        '{"a":"' + "a" * 64 + '","b":"' + "A" * 64 + '"}',
        '{"a":"' + "a" * 64 + '","a":"' + "b" * 64 + '"}',
    ],
)
def test_invalid_mapping_fails_closed(client, ready_review, identities, monkeypatch, bad):
    monkeypatch.setenv("TEACHER_REVIEW_TOKEN_HASHES", bad)
    response = client.post(path(ready_review), json=body(ready_review), headers=identities["a"])
    assert response.status_code == 503
    assert bad not in response.text
    assert client.get("/teacher/sessions", headers=AUTH).status_code == 200


def test_shared_token_cannot_alias_teacher(client, ready_review, identities, monkeypatch):
    import os

    admin_hash = hashlib.sha256(os.environ["TEACHER_MANAGEMENT_TOKEN"].encode()).hexdigest()
    monkeypatch.setenv("TEACHER_REVIEW_TOKEN_HASHES", json.dumps({"teacher-a": admin_hash}))
    assert client.post(path(ready_review), json=body(ready_review), headers=AUTH).status_code == 403
    assert (
        client.post(
            path(ready_review), json=body(ready_review), headers=identities["a"]
        ).status_code
        == 503
    )
    assert client.get("/teacher/sessions", headers=AUTH).status_code == 200


@pytest.mark.parametrize("ready_review", [{"version": "v1", "default_ratio": 1}], indirect=True)
def test_two_teachers_zero_override_and_retry(db, client, ready_review, identities):
    c = ready_review
    first = client.post(
        path(c), json=body(c), headers={**identities["a"], "X-Teacher-Id": "teacher-b"}
    )
    assert first.status_code == 200
    data = first.json()
    assert data["revision"] == 1 and data["session_status"] == "INPUT_CLOSED"
    assert not data["final_result_created"] and data["reviewed_paper_score_id"]
    second_body = body(c, index=1, operation="request-2", revision=1, action="OVERRIDE", value=0)
    second = client.post(path(c, 1), json=second_body, headers=identities["b"])
    assert second.status_code == 200 and second.json()["session_status"] == "COMPLETED"
    assert second.json()["final_result_created"] and second.json()["final_result_id"]
    calls = list(c["plugin"].calls)
    retry = client.post(path(c, 1), json=second_body, headers=identities["b"])
    assert retry.status_code == 200 and retry.json() == second.json()
    assert c["plugin"].calls == calls == ["task", "task", "paper", "paper", "paper"]
    app.dependency_overrides.pop(get_scoring_registry)
    assert client.post(path(c, 1), json=second_body, headers=identities["b"]).status_code == 200
    assert c["plugin"].calls == calls
    assert c["adapter"].calls == 2
    assert (
        client.post(
            path(c, 1), json={**second_body, "override_value": 2}, headers=identities["b"]
        ).status_code
        == 409
    )
    assert (
        client.post(
            path(c), json=body(c, operation="late", revision=2), headers=identities["a"]
        ).status_code
        == 409
    )
    with Session(db) as s:
        decisions = list(s.scalars(select(ReviewDecision).order_by(ReviewDecision.revision)))
        assert [d.teacher_id for d in decisions] == ["teacher-a", "teacher-b"]
        assert decisions[1].override_value == 0
        assert s.scalar(select(func.count()).select_from(ReviewedPaperScore)) == 2
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 1


def test_eligibility_validation_and_capability(client, ready_review, identities):
    c = ready_review
    assert (
        client.post(
            path(c), json={**body(c), "submission_id": str(uuid4())}, headers=identities["a"]
        ).status_code
        == 409
    )
    assert (
        client.post(path(c, 1), json=body(c, index=1), headers=identities["a"]).status_code == 409
    )
    assert (
        client.post(
            path(c),
            json={**body(c), "submission_id": str(c["submissions"][1])},
            headers=identities["a"],
        ).status_code
        == 409
    )
    assert (
        client.post(
            path(c), json=body(c, action="OVERRIDE", value=None), headers=identities["a"]
        ).status_code
        == 422
    )
    assert (
        client.post(
            path(c), json=body(c, action="OVERRIDE", value=-1), headers=identities["a"]
        ).status_code
        == 422
    )
    assert (
        client.post(
            path(c), json={**body(c), "expected_revision": True}, headers=identities["a"]
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/teacher/sessions/{uuid4()}/reviews/{uuid4()}/decisions",
            json=body(c),
            headers=identities["a"],
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/teacher/sessions/{c['sid']}/reviews/{uuid4()}/decisions",
            json=body(c),
            headers=identities["a"],
        ).status_code
        == 404
    )
    app.dependency_overrides.pop(get_scoring_registry)
    assert client.post(path(c), json=body(c), headers=identities["a"]).status_code == 503


@pytest.mark.parametrize("ready_review", [{"version": "v1", "default_ratio": 1}], indirect=True)
def test_concurrent_review_revision(db, client, ready_review, identities):
    c = ready_review

    def run(i):
        return client.post(
            path(c, i),
            json=body(c, index=i, operation=f"op-{i}"),
            headers=identities["a" if i == 0 else "b"],
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == [200, 409]
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 1
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"


def test_atomic_rollback_if_settlement_fails(db, client, ready_review, identities, monkeypatch):
    c = ready_review

    from app.scoring.settlement import settle_session

    def fail(*args, **kwargs):
        assert settle_session(*args, **kwargs) is not None
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("app.api.teacher_review.settle_session", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        client.post(path(c), json=body(c), headers=identities["a"])
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 0
        assert s.scalar(select(func.count()).select_from(ReviewedPaperScore)) == 0
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 0
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"


def test_individual_queries_work_without_management_configuration(
    client, ready_review, identities, monkeypatch
):
    monkeypatch.delenv("TEACHER_MANAGEMENT_TOKEN")
    assert client.get("/teacher/sessions", headers=identities["a"]).status_code == 200
    assert client.get("/teacher/banks", headers=identities["a"]).status_code == 503
    assert (
        client.post(
            path(ready_review), json=body(ready_review), headers=identities["a"]
        ).status_code
        == 200
    )


def test_missing_override_capability_is_503_without_writes(db, client, ready_review, identities):
    ready_review["plugin"].apply_override = None
    response = client.post(
        path(ready_review),
        json=body(ready_review, action="OVERRIDE", value=0),
        headers=identities["a"],
    )
    assert response.status_code == 503
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 0


def test_old_submission_cannot_be_reviewed(db, client, context, tmp_path, ready_review, identities):
    from test_evaluation_worker import claim
    from test_score_review import ReviewAdapter
    from test_submission import deliver, submit

    from app.evaluation.models import EvaluationJob
    from app.evaluation.worker import execute_claim, schedule
    from app.media.fixed_audio import AudioStore
    from app.sessions.assessment_session import (
        create_session,
        ordered_tasks,
        redo_attempt,
        start_attempt,
    )
    from app.sessions.close_manifest import close_session

    registry = ready_review["registry"]
    with Session(db) as s, s.begin():
        paper_id = s.get(AssessmentSession, context["session_id"]).paper_id
        owner = create_session(
            s,
            paper_id=paper_id,
            scope=str(uuid4()),
            create_request_id="create",
            review_policy={"version": "v1", "default_ratio": 1},
            store=AudioStore(tmp_path),
            registry=registry,
        )
        task = ordered_tasks(s, owner.session_id)[0]
        attempt = start_attempt(
            s, session_id=owner.session_id, session_task_id=task.session_task_id
        )
        first = {
            "session_id": owner.session_id,
            "session_task_id": task.session_task_id,
            "attempt_id": attempt.attempt_id,
        }
    deliver(db, first)
    with Session(db) as s, s.begin():
        old_id = submit(s, first).submission_id
        attempt = redo_attempt(
            s,
            session_id=first["session_id"],
            session_task_id=first["session_task_id"],
            expected_attempt_id=first["attempt_id"],
            redo_request_id="redo",
        )
        second = {**first, "attempt_id": attempt.attempt_id}
    deliver(db, second)
    with Session(db) as s, s.begin():
        new_id = submit(s, second, request="new", choice="B").submission_id
        schedule(s)
        new_job = s.scalar(
            select(EvaluationJob.job_id).where(EvaluationJob.submission_id == new_id)
        )
        close_session(s, session_id=first["session_id"], close_request_id="close")
    assert execute_claim(db, claim(db, new_job, ReviewAdapter()))
    from app.scoring.settlement import settle_session

    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=first["session_id"], registry=registry) is None
    url = f"/teacher/sessions/{first['session_id']}/reviews/{first['session_task_id']}/decisions"
    request = {
        "submission_id": str(old_id),
        "operation_id": "old",
        "expected_revision": 0,
        "action": "CONFIRM",
        "override_value": None,
    }
    assert client.post(url, json=request, headers=identities["a"]).status_code == 409
    request["submission_id"] = str(new_id)
    assert client.post(url, json=request, headers=identities["a"]).status_code == 200
