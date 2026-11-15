from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_evaluation_worker import FakeAdapter, claim
from test_score_review import decide
from test_score_settlement import prepare
from test_submission import deliver, submit
from test_teacher_api import AUTH

from app.api.teacher_scoring import safe
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import execute_claim, schedule
from app.scoring.models import AutomaticTaskScore, FinalScoreResult, ReviewDecision
from app.scoring.settlement import settle_session
from app.sessions.close_manifest import close_session

pytest_plugins = ("test_teacher_api", "test_score_review")


def test_auth_and_missing_session(client, monkeypatch):
    sid = str(uuid4())
    urls = [
        "/teacher/sessions",
        *(f"/teacher/sessions/{sid}/{part}" for part in ("scoring", "reviews", "result")),
    ]
    for url in urls:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        if url != urls[0]:
            assert client.get(url, headers=AUTH).status_code == 404
    monkeypatch.delenv("TEACHER_MANAGEMENT_TOKEN")
    for url in urls:
        assert client.get(url, headers=AUTH).status_code == 503


def test_unclosed_and_final_no_review(db, client, two_tasks, tmp_path):
    old_sid = two_tasks[0]["session_id"]
    initial = client.get(f"/teacher/sessions/{old_sid}/scoring", headers=AUTH).json()
    assert initial["progress"] == "IN_PROGRESS" and "INPUT_NOT_CLOSED" in initial["blockers"]
    assert client.get(f"/teacher/sessions/{old_sid}/result", headers=AUTH).json()["ready"] is False
    sid, mid, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    before = client.get(f"/teacher/sessions/{sid}/result", headers=AUTH).json()
    assert before["ready"] is False and before["paper_score"] is None
    with Session(db) as s, s.begin():
        settle_session(s, session_id=sid, registry=registry)
    calls = list(plugin.calls)
    out = client.get(f"/teacher/sessions/{sid}/result", headers=AUTH).json()
    assert out["ready"] is True and out["manifest_id"] == str(mid)
    assert out["paper_score"]["result"]["value"] == {"total": 1}
    assert [t["position"] for t in out["tasks"]] == [1, 2]
    assert sorted(t["automatic_score"]["result"]["value"]["score"] for t in out["tasks"]) == [0, 1]
    assert plugin.calls == calls
    assert "reference_answer" not in str(out) and "inputs" not in str(out)
    page = client.get("/teacher/sessions?status=COMPLETED&limit=1&offset=0", headers=AUTH).json()
    assert page["total"] == 1 and page["items"][0]["session_id"] == str(sid)
    assert (
        client.get("/teacher/sessions?status=COMPLETED&limit=1&offset=1", headers=AUTH).json()[
            "items"
        ]
        == []
    )


@pytest.mark.parametrize(
    "mode,expected",
    [(None, "WAITING"), ("failure", "FAILED"), ("invalid", "INVALID"), ("unknown", "UNKNOWN")],
)
def test_evaluation_states(db, client, context, mode, expected):
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
        schedule(s)
        jid = s.scalar(select(EvaluationJob.job_id))
        close_session(s, session_id=context["session_id"], close_request_id="close")
    if mode:
        adapter = FakeAdapter(mode)
        assert execute_claim(db, claim(db, jid, adapter))
        before = adapter.calls
    out = client.get(f"/teacher/sessions/{context['session_id']}/scoring", headers=AUTH).json()
    task = out["tasks"][0]
    assert task["evaluations"][0]["status"] == expected
    assert "EVALUATION_" + expected in out["blockers"]
    assert task["automatic_score"] is None and task["effective_score"] is None
    assert "payload" not in str(out) and "input_refs" not in str(out)
    if mode:
        assert adapter.calls == before


def test_review_history_and_final_snapshot_are_read_only(db, client, ready_review):
    c = ready_review
    sid = c["sid"]
    url = f"/teacher/sessions/{sid}"
    before = client.get(url + "/reviews", headers=AUTH).json()
    assert len(before["tasks"]) == 1
    assert before["tasks"][0]["review_reason"] == "MODEL_RECOMMENDED"
    assert before["tasks"][0]["review_status"] == "PENDING"
    assert before["tasks"][0]["review_qualification"] == "UNVERIFIED_SOURCE"
    assert "REVIEW_PENDING" in before["blockers"]
    with Session(db) as s, s.begin():
        decide(s, c, action="OVERRIDE", value=2)
    with Session(db) as s, s.begin():
        decide(s, c, operation="second", revision=1, action="OVERRIDE", value=3)
        final_id = settle_session(s, session_id=sid, registry=c["registry"]).result_id
    calls, external_calls = list(c["plugin"].calls), c["adapter"].calls
    with Session(db) as s:
        counts = [
            s.scalar(select(func.count()).select_from(model))
            for model in (AutomaticTaskScore, ReviewDecision, RawEvaluation, FinalScoreResult)
        ]
    review = client.get(url + "/reviews", headers=AUTH).json()
    assert review["expected_review_revision"] == 2
    assert [d["revision"] for d in review["tasks"][0]["history"]] == [1, 2]
    assert review["tasks"][0]["effective_score"]["value"] == {"adjusted": 3}
    assert review["preview"]["result"]["value"] == {"total": 9}
    final = client.get(url + "/result", headers=AUTH).json()
    assert final["result_id"] == str(final_id) and final["paper_score"]["revision"] == 2
    assert final["paper_score"]["result"]["value"] == {"total": 9}
    assert final["tasks"][0]["effective_score"]["value"] == {"adjusted": 3}
    assert final["tasks"][0]["automatic_score"]["result"]["value"] == {"adjusted": 1}
    assert c["plugin"].calls == calls and c["adapter"].calls == external_calls
    with Session(db) as s:
        assert counts == [
            s.scalar(select(func.count()).select_from(model))
            for model in (AutomaticTaskScore, ReviewDecision, RawEvaluation, FinalScoreResult)
        ]
    for forbidden in (
        "reference_answer",
        "scoring_instruction",
        "storage_ref",
        "review_reasons",
        "payload",
        "input_refs",
        "teacher_id",
    ):
        assert forbidden not in str(review) + str(final)


def test_redaction_is_conservative():
    result = safe(
        {
            "score": 0,
            "total": 2,
            "comment": "private",
            "prompt": "secret",
            "foo": 33,
            "dimensions": {"semantic": 1, "secret": "hidden"},
        }
    ).model_dump()
    assert result == {
        "value": {"score": 0, "total": 2, "dimensions": {"semantic": 1}},
        "redacted": True,
    }
    assert safe("private free text").model_dump() == {"value": None, "redacted": True}


@pytest.mark.parametrize("ready_review", [{"version": "v1", "default_ratio": 1}], indirect=True)
def test_sampling_and_combined_reasons(client, ready_review):
    c = ready_review
    body = client.get(f"/teacher/sessions/{c['sid']}/reviews", headers=AUTH).json()
    reasons = {item["review_reason"] for item in body["tasks"]}
    assert reasons == {"SAMPLED", "SAMPLED+MODEL_RECOMMENDED"}
    assert all(item["review_id"] for item in body["tasks"])
    assert body["expected_review_revision"] == 0


def test_inconsistent_legacy_pending_is_diagnostic(db, client, ready_review):
    from app.scoring.models import PendingReview

    c = ready_review
    with Session(db) as s, s.begin():
        s.add(
            PendingReview(
                review_id=uuid4(),
                manifest_id=c["manifest"],
                session_task_id=c["contexts"][1]["session_task_id"],
                reason="SAMPLED",
            )
        )
    response = client.get(f"/teacher/sessions/{c['sid']}/reviews", headers=AUTH).json()
    assert len(response["tasks"]) == 2
    assert "REVIEW_QUALIFICATION_INCONSISTENT" in response["blockers"]
    assert any(t["review_qualification"] == "INCONSISTENT_SAMPLING" for t in response["tasks"])


def test_http_score_result_redacts_text_and_unknown_fields(db, client, two_tasks, tmp_path):
    sid, _, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    plugin.score_task = lambda *args: {
        "score": 0,
        "prompt": "PRIVATE_PROMPT",
        "comment": "PRIVATE_TEXT",
        "unknown": 345,
    }
    plugin.score_paper = lambda *args: {"total": 0, "reference_answer": "SECRET_M"}
    with Session(db) as s, s.begin():
        settle_session(s, session_id=sid, registry=registry)
    response = client.get(f"/teacher/sessions/{sid}/result", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["paper_score"]["result"] == {"value": {"total": 0}, "redacted": True}
    assert body["tasks"][0]["automatic_score"]["result"] == {
        "value": {"score": 0},
        "redacted": True,
    }
    for token in ("PRIVATE_PROMPT", "PRIVATE_TEXT", "SECRET_M", "unknown", "reference_answer"):
        assert token not in response.text


def test_paging_sort_and_status_filter(db, client, two_tasks, tmp_path):
    first, _, _, _ = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    second, _, _, _ = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    first_page = client.get(
        "/teacher/sessions?status=INPUT_CLOSED&limit=1&offset=0", headers=AUTH
    ).json()
    second_page = client.get(
        "/teacher/sessions?status=INPUT_CLOSED&limit=1&offset=1", headers=AUTH
    ).json()
    assert first_page["total"] == second_page["total"] == 2
    assert [first_page["items"][0]["session_id"], second_page["items"][0]["session_id"]] == [
        str(second),
        str(first),
    ]
    assert (
        client.get("/teacher/sessions?status=INPUT_CLOSED&limit=1&offset=2", headers=AUTH).json()[
            "items"
        ]
        == []
    )


def test_gets_never_execute_business_or_source_callbacks(db, client, ready_review, monkeypatch):
    from sqlalchemy import event

    c = ready_review

    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not execute scoring or evaluation")

    monkeypatch.setattr("app.scoring.settlement.settle_session", forbidden)
    monkeypatch.setattr("app.scoring.review.record_review", forbidden)
    monkeypatch.setattr("app.scoring.registry.Registry.review_recommendation", forbidden)
    monkeypatch.setattr(c["plugin"], "score_task", forbidden)
    monkeypatch.setattr(c["plugin"], "score_paper", forbidden)
    monkeypatch.setattr(c["adapter"], "evaluate", forbidden)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split(None, 1)[0].upper())

    event.listen(db, "before_cursor_execute", capture)
    try:
        for suffix in ("scoring", "reviews", "result"):
            assert (
                client.get(f"/teacher/sessions/{c['sid']}/{suffix}", headers=AUTH).status_code
                == 200
            )
        page = client.get("/teacher/sessions?status=INPUT_CLOSED", headers=AUTH).json()
        assert page["items"][0]["progress"] == "WAITING_REVIEW"
    finally:
        event.remove(db, "before_cursor_execute", capture)
    assert statements and set(statements).issubset({"SET", "SELECT"})
