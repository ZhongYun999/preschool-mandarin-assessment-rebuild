"""HTTP-to-worker settlement acceptance with isolated PostgreSQL facts."""

import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_assessment_http import access, create_body, issue
from test_evaluation_worker import FakeAdapter
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG
from test_score_review import NonlinearPlugin
from test_taskbank import CONTENT, write
from test_teacher_api import AUTH

from app.api.teacher_api import get_audio_store
from app.api.teacher_review import get_scoring_registry
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import AdapterResult
from app.evaluation.worker import run_once as run_evaluation
from app.main import app
from app.media.fixed_audio import AudioStore, prepare_audio
from app.papers.paper import add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.models import (
    AutomaticPaperScore,
    AutomaticTaskScore,
    FinalScoreResult,
    PendingReview,
    ReviewDecision,
    ReviewedPaperScore,
)
from app.scoring.registry import EvaluationContract, Registry
from app.scoring.worker import run_settlement_once
from app.sessions.assessment_session import AssessmentSession
from app.sessions.close_manifest import CloseManifest, CloseManifestEntry
from app.sessions.submission import EvaluationIntent

pytest_plugins = ("test_assessment_http",)


class Adapter(FakeAdapter):
    def __init__(self, recommendation=False):
        super().__init__()
        self.recommendation = recommendation

    def evaluate(self, inputs, requirement):
        self.calls += 1
        return AdapterResult(payload={"metric": 0, "recommendation": self.recommendation})


@pytest.fixture
def flow(db, bank, tmp_path, client, monkeypatch):
    def make(*, required=False, ratio=0, recommendation=False, redo_first=False):
        plugin = NonlinearPlugin()
        if not required:
            plugin.evaluation_requirements["instruction"] = ()
        contract = EvaluationContract(
            "fake",
            "1",
            frozenset({"instruction"}),
            lambda p: p == {},
            (
                None
                if recommendation is None
                else False
                if recommendation is False
                else lambda payload: payload["recommendation"]
            ),
        )
        registry = Registry([plugin], [contract] if required else [])
        paper = draft(db, bank)
        tasks = [
            write(
                db,
                bank,
                replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="A"),
                code=f"{paper}-{i}",
            )
            for i in range(2)
        ]
        with Session(db) as s, s.begin():
            for i, task in enumerate(tasks):
                add_entry(
                    s, paper_id=paper, expected_version=i, task_id=task.task_id, position=i + 1
                )
            select_plugin(
                s,
                paper_id=paper,
                expected_version=2,
                plugin_id="test-only",
                version="1",
                registry=registry,
            )
            publish(
                s,
                paper_id=paper,
                expected_version=3,
                registry=registry,
                audio_config=TEST_AUDIO_CONFIG,
            )
        store = AudioStore(tmp_path)
        prepare_audio(db, paper, store, Synth())
        app.dependency_overrides[get_audio_store] = lambda: store
        app.dependency_overrides[get_scoring_registry] = lambda: registry
        token = secrets.token_urlsafe(32)
        monkeypatch.setenv(
            "TEACHER_REVIEW_TOKEN_HASHES",
            json.dumps({"teacher": hashlib.sha256(token.encode()).hexdigest()}),
        )
        teacher = {"Authorization": "Bearer " + token}
        response = client.post(
            "/teacher/assessment/sessions",
            headers=AUTH,
            json={**create_body(paper), "review_policy": {"version": "v1", "default_ratio": ratio}},
        )
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        headers = access(issue(client, sid))
        root = f"/assessment/sessions/{sid}"
        state = client.get(root, headers=headers).json()
        submissions = []
        for task, choice in zip(state["tasks"], ("A", "B"), strict=True):
            base = f"{root}/tasks/{task['session_task_id']}"
            content = client.get(base + "/content", headers=headers).json()
            assert client.get(content["audio"]["prompt"], headers=headers).status_code == 200
            attempt = client.post(base + "/attempt", headers=headers).json()["attempt_id"]
            for kind in ("GUIDE", "INITIAL_PROMPT", "USER_REPLAY"):
                started = client.post(
                    base + "/playbacks",
                    headers=headers,
                    json={"attempt_id": attempt, "request_id": kind, "kind": kind},
                ).json()
                assert (
                    client.post(
                        base + "/playbacks/" + started["playback_id"] + "/finish",
                        headers=headers,
                        json={"attempt_id": attempt, "completed": True},
                    ).status_code
                    == 200
                )
            body = {
                "attempt_id": attempt,
                "submit_request_id": "submit",
                "selected_choice_id": choice,
            }
            accepted = client.post(base + "/submissions/instruction", headers=headers, json=body)
            assert accepted.status_code == 200
            assert (
                client.post(base + "/submissions/instruction", headers=headers, json=body).json()
                == accepted.json()
            )
            submissions.append(accepted.json()["submission_id"])
        old_submission = None
        if redo_first:
            old_submission = submissions[0]
            base = f"{root}/tasks/{state['tasks'][0]['session_task_id']}"
            attempt = client.post(
                base + "/redo", headers=headers, json={"redo_request_id": "redo"}
            ).json()["attempt_id"]
            for kind in ("GUIDE", "INITIAL_PROMPT"):
                row = client.post(
                    base + "/playbacks",
                    headers=headers,
                    json={"attempt_id": attempt, "request_id": kind, "kind": kind},
                ).json()
                assert (
                    client.post(
                        base + "/playbacks/" + row["playback_id"] + "/finish",
                        headers=headers,
                        json={"attempt_id": attempt, "completed": True},
                    ).status_code
                    == 200
                )
            result = client.post(
                base + "/submissions/instruction",
                headers=headers,
                json={
                    "attempt_id": attempt,
                    "submit_request_id": "replacement",
                    "selected_choice_id": "A",
                },
            )
            assert result.status_code == 200
            submissions[0] = result.json()["submission_id"]
        closed = client.post(root + "/close", headers=headers, json={"close_request_id": "close"})
        assert closed.status_code == 200 and closed.json()["status"] == "INPUT_CLOSED"
        assert (
            client.post(root + "/close", headers=headers, json={"close_request_id": "close"}).json()
            == closed.json()
        )
        return {
            "sid": sid,
            "headers": headers,
            "teacher": teacher,
            "root": root,
            "tasks": state["tasks"],
            "submissions": submissions,
            "manifest": closed.json()["manifest_id"],
            "plugin": plugin,
            "registry": registry,
            "adapter": Adapter(recommendation),
            "required": required,
            "old_submission": old_submission,
        }

    return make


def evaluate(db, f):
    return run_evaluation(
        db,
        registry=f["registry"],
        adapters={("fake", "1"): f["adapter"]},
        lease=timedelta(minutes=2),
        batch_size=100,
    )


def count(db, model):
    with Session(db) as s:
        return s.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("required", [False, True])
def test_full_no_review(db, client, flow, required):
    f = flow(required=required)
    assert client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH).json()["ready"] is False
    assert (
        client.get("/teacher/sessions/" + f["sid"] + "/result", headers=f["headers"]).status_code
        == 401
    )
    assert count(db, FinalScoreResult) == 0
    if required:
        pending = run_settlement_once(db, registry=f["registry"], batch_size=1)
        assert len(pending["pending"]) == 1 and count(db, AutomaticTaskScore) == 0
        db.dispose()  # A new connection resumes from persisted intent and submission facts.
    result = evaluate(db, f)
    assert result["finished"] == (2 if required else 0)
    assert count(db, EvaluationJob) == (2 if required else 0)
    assert count(db, RawEvaluation) == (2 if required else 0)
    outcome = run_settlement_once(db, registry=f["registry"], batch_size=1)
    assert len(outcome["completed"]) == 1 and not outcome["failures"]
    assert count(db, AutomaticTaskScore) == 2 and count(db, AutomaticPaperScore) == 1
    assert count(db, FinalScoreResult) == 1 and count(db, PendingReview) == 0
    assert count(db, EvaluationIntent) == (2 if required else 0)
    assert count(db, CloseManifest) == 1
    assert client.get(f["root"], headers=f["headers"]).json()["status"] == "COMPLETED"
    formal = client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH).json()
    assert formal["ready"] and formal["paper_score"]["result"]["value"]["total"] == 1
    calls = list(f["plugin"].calls)
    evaluate(db, f)
    assert not run_settlement_once(db, registry=f["registry"])["completed"]
    client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH)
    assert f["plugin"].calls == calls == ["task", "task", "paper"]


def test_http_review_nonlinear_and_idempotency(db, client, flow):
    f = flow(required=True, ratio=1)
    evaluate(db, f)
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    assert count(db, PendingReview) == 2
    assert (
        client.post("/teacher/banks", headers=f["teacher"], json={"name": "x"}).status_code == 401
    )
    first_path = (
        f"/teacher/sessions/{f['sid']}/reviews/{f['tasks'][0]['session_task_id']}/decisions"
    )
    first_body = {
        "submission_id": f["submissions"][0],
        "operation_id": "review1",
        "expected_revision": 0,
        "action": "CONFIRM",
        "override_value": None,
    }
    assert client.post(first_path, headers=AUTH, json=first_body).status_code == 403
    assert client.post(first_path, headers=f["teacher"], json=first_body).status_code == 200
    second_path = (
        f"/teacher/sessions/{f['sid']}/reviews/{f['tasks'][1]['session_task_id']}/decisions"
    )
    second_body = {
        "submission_id": f["submissions"][1],
        "operation_id": "review2",
        "expected_revision": 1,
        "action": "OVERRIDE",
        "override_value": 3,
    }
    decision = client.post(second_path, headers=f["teacher"], json=second_body)
    assert decision.status_code == 200 and decision.json()["session_status"] == "COMPLETED"
    assert (
        client.post(second_path, headers=f["teacher"], json=second_body).json() == decision.json()
    )
    assert count(db, ReviewDecision) == 2 and count(db, ReviewedPaperScore) == 2
    with Session(db) as s:
        final = s.scalar(select(FinalScoreResult))
        reviewed = s.get(ReviewedPaperScore, final.reviewed_paper_score_id)
        decisions = list(s.scalars(select(ReviewDecision).order_by(ReviewDecision.revision)))
        assert reviewed.revision == 2
        assert [ref["review_decision_id"] for ref in reviewed.inputs] == [
            str(d.decision_id) for d in decisions
        ]
    formal = client.get(f"/teacher/sessions/{f['sid']}/result", headers=AUTH).json()
    assert formal["ready"] and formal["paper_score"]["result"]["value"]["total"] == 19
    assert [t["automatic_score"]["result"]["value"]["adjusted"] for t in formal["tasks"]] == [1, 0]
    assert [t["effective_score"]["value"]["adjusted"] for t in formal["tasks"]] == [1, 3]
    assert f["plugin"].calls == ["task", "task", "paper", "paper", "paper"]
    assert not run_settlement_once(db, registry=f["registry"])["completed"]
    assert f["adapter"].calls == 2


def test_recommendation_and_unknown(db, flow):
    f = flow(required=True, ratio=1, recommendation=True)
    evaluate(db, f)
    run_settlement_once(db, registry=f["registry"])
    with Session(db) as s:
        reasons = list(s.scalars(select(PendingReview.reason)))
        assert reasons == ["SAMPLED+MODEL_RECOMMENDED"] * 2
    assert count(db, PendingReview) == 2


@pytest.mark.parametrize("recommendation,expected_reviews", [(True, 2), (None, 0)])
def test_recommendation_zero_sampling_and_unknown(db, flow, recommendation, expected_reviews):
    f = flow(required=True, ratio=0, recommendation=recommendation)
    evaluate(db, f)
    result = run_settlement_once(db, registry=f["registry"])
    assert len(result["pending"]) == 1 and count(db, FinalScoreResult) == 0
    assert count(db, PendingReview) == expected_reviews
    if recommendation:
        with Session(db) as s:
            assert set(s.scalars(select(PendingReview.reason))) == {"MODEL_RECOMMENDED"}
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    assert f["plugin"].calls == ["task", "task", "paper"]


def test_scan_fairness_failure_and_transaction_recovery(db, two_tasks, tmp_path, monkeypatch):
    from test_score_settlement import prepare

    from app.scoring import worker

    bad, _, _, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    good, _, _, _ = prepare(db, two_tasks, tmp_path, {"version": "v1", "default_ratio": 0})
    original = worker.settle_session

    def fail_after_scoring(session, *, session_id, registry):
        value = original(session, session_id=session_id, registry=registry)
        if session_id == bad:
            raise RuntimeError("private data not returned")
        return value

    monkeypatch.setattr(worker, "settle_session", fail_after_scoring)
    result = worker.run_settlement_once(db, registry=registry, batch_size=1)
    assert [item["session_id"] for item in result["completed"]] == [good]
    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["session_id"] == bad and failure["error_type"] == "RuntimeError"
    assert failure["location"]["function"] == "fail_after_scoring"
    with Session(db) as s:
        assert s.get(AssessmentSession, bad).status == "INPUT_CLOSED"
        assert s.get(AssessmentSession, good).status == "COMPLETED"
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 1
        assert s.scalar(select(func.count()).select_from(AutomaticTaskScore)) == 2
    monkeypatch.setattr(worker, "settle_session", original)
    assert len(worker.run_settlement_once(db, registry=registry, batch_size=1)["completed"]) == 1
    assert count(db, FinalScoreResult) == 2


def test_unknown_historical_policy_does_not_starve_ready(db, two_tasks, tmp_path):
    from test_score_settlement import prepare

    waiting, _, _, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0.25})
    ready, _, _, _ = prepare(db, two_tasks, tmp_path, {"version": "v1", "default_ratio": 0})
    out = run_settlement_once(db, registry=registry, batch_size=1)
    assert out["pending"][0]["session_id"] == waiting
    assert out["pending"][0]["diagnostic"]
    assert out["completed"][0]["session_id"] == ready
    assert not out["failures"]
    out = run_settlement_once(db, registry=registry, batch_size=1)
    assert out["pending"][0]["session_id"] == waiting and not out["completed"]


def test_simultaneous_workers_skip_locked_without_duplicate_scoring(db, two_tasks, tmp_path):
    from threading import Event

    from test_score_settlement import prepare

    sid, _, plugin, registry = prepare(
        db, two_tasks, tmp_path, {"version": "v1", "default_ratio": 0}
    )
    entered, release = Event(), Event()
    original = plugin.score_task

    def held(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    plugin.score_task = held
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_settlement_once, db, registry=registry)
        assert entered.wait(10)
        second = pool.submit(run_settlement_once, db, registry=registry)
        other = second.result(timeout=10)
        release.set()
        done = first.result(timeout=10)
    assert other["skipped_locked"] == [sid]
    assert len(done["completed"]) == 1 and count(db, FinalScoreResult) == 1
    assert plugin.calls == ["task", "task", "paper"]


def test_last_review_and_worker_race(db, client, flow):
    f = flow(required=True, ratio=1)
    evaluate(db, f)
    run_settlement_once(db, registry=f["registry"])

    def review(index, revision):
        path = (
            f"/teacher/sessions/{f['sid']}/reviews/{f['tasks'][index]['session_task_id']}/decisions"
        )
        return client.post(
            path,
            headers=f["teacher"],
            json={
                "submission_id": f["submissions"][index],
                "operation_id": f"op{index}",
                "expected_revision": revision,
                "action": "CONFIRM",
            },
        )

    assert review(0, 0).status_code == 200
    barrier = Barrier(2)

    def last():
        barrier.wait()
        return review(1, 1)

    def scan():
        barrier.wait()
        return run_settlement_once(db, registry=f["registry"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(last), pool.submit(scan)
        assert a.result().status_code == 200
        assert not b.result()["failures"]
    assert count(db, FinalScoreResult) == 1 and count(db, ReviewDecision) == 2
    assert f["plugin"].calls == ["task", "task", "paper", "paper", "paper"]


@pytest.mark.parametrize(
    "operation,first",
    [("submit", "submit"), ("submit", "close"), ("redo", "redo"), ("redo", "close")],
)
def test_http_close_competing_with_submit_or_redo(
    db, client, two_tasks, monkeypatch, operation, first
):
    from threading import Event

    from test_submission import deliver, submit

    import app.api.assessment_operations as api
    from app.sessions.close_manifest import IncompleteSession

    one, two = two_tasks
    for ctx in (one, two):
        deliver(db, ctx)
    with Session(db) as s, s.begin():
        first_submission = submit(s, one).submission_id
        if operation == "redo":
            submit(s, two)
    sid = str(one["session_id"])
    headers = access(issue(client, sid))
    root = f"/assessment/sessions/{sid}"
    task_root = f"{root}/tasks/{two['session_task_id']}"
    submit_body = {
        "attempt_id": str(two["attempt_id"]),
        "submit_request_id": "submit",
        "selected_choice_id": "A",
    }
    entered, release = Event(), Event()
    function_name = {
        "submit": "submit_instruction",
        "redo": "redo_attempt",
        "close": "close_session",
    }[first]
    original = getattr(api, function_name)

    def held(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except IncompleteSession:
            entered.set()
            assert release.wait(10)
            raise
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(api, function_name, held)

    def call(name):
        if name == "close":
            return client.post(root + "/close", headers=headers, json={"close_request_id": "close"})
        if name == "redo":
            return client.post(
                task_root + "/redo", headers=headers, json={"redo_request_id": "redo"}
            )
        return client.post(
            task_root + "/submissions/instruction", headers=headers, json=submit_body
        )

    second = "close" if first != "close" else operation
    from sqlalchemy import event

    competing_lock = Event()

    def observe_lock(conn, cursor, statement, parameters, context, executemany):
        if "assessment_sessions" in statement and "FOR UPDATE" in statement:
            competing_lock.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(call, first)
        try:
            assert entered.wait(10)
            event.listen(db, "before_cursor_execute", observe_lock)
            b = pool.submit(call, second)
            assert competing_lock.wait(10)
        finally:
            event.remove(db, "before_cursor_execute", observe_lock)
            release.set()
        responses = {first: a.result(timeout=10), second: b.result(timeout=10)}
    if operation == "submit":
        assert responses["submit"].status_code == 200
        assert responses["close"].status_code == (200 if first == "submit" else 409)
    else:
        assert responses["redo"].status_code == (200 if first == "redo" else 409)
        assert responses["close"].status_code == (409 if first == "redo" else 200)
    with Session(db) as s:
        manifest = s.scalar(
            select(CloseManifest).where(CloseManifest.session_id == one["session_id"])
        )
        if responses["close"].status_code == 200:
            selected = list(
                s.scalars(
                    select(CloseManifestEntry).where(
                        CloseManifestEntry.manifest_id == manifest.manifest_id
                    )
                )
            )
            assert len(selected) == 2
            assert first_submission in {entry.submission_id for entry in selected}
            if operation == "submit":
                assert UUID(responses["submit"].json()["submission_id"]) in {
                    entry.submission_id for entry in selected
                }
        else:
            assert manifest is None


def test_review_requires_evidence_and_ignores_superseded_submission(db, client, flow):
    f = flow(required=True, ratio=1, recommendation=True, redo_first=True)
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    path = f"/teacher/sessions/{f['sid']}/reviews/{f['tasks'][0]['session_task_id']}/decisions"
    body = {
        "submission_id": f["submissions"][0],
        "operation_id": "review",
        "expected_revision": 0,
        "action": "CONFIRM",
    }
    assert client.post(path, headers=f["teacher"], json=body).status_code == 409
    assert count(db, AutomaticTaskScore) == 0 and count(db, PendingReview) == 0
    assert evaluate(db, f)["finished"] == 2
    run_settlement_once(db, registry=f["registry"])
    with Session(db) as s:
        entries = list(s.scalars(select(CloseManifestEntry)))
        assert UUID(f["old_submission"]) not in {entry.submission_id for entry in entries}
        assert {str(entry.submission_id) for entry in entries} == set(f["submissions"])
        assert UUID(f["old_submission"]) not in set(
            s.scalars(select(AutomaticTaskScore.submission_id))
        )
        assert s.scalar(select(func.count()).select_from(PendingReview)) == 2
        old_job = s.scalar(
            select(EvaluationJob).where(EvaluationJob.submission_id == UUID(f["old_submission"]))
        )
        assert old_job.status == "SKIPPED"
    assert (
        client.post(
            path, headers=f["teacher"], json={**body, "submission_id": f["old_submission"]}
        ).status_code
        == 409
    )


def test_worker_restart_in_fresh_process(db, flow):
    import os
    import subprocess
    import sys
    from pathlib import Path

    f = flow(required=True)
    assert len(run_settlement_once(db, registry=f["registry"])["pending"]) == 1
    evaluate(db, f)
    with db.connect() as connection:
        schema = connection.scalar(select(func.current_schema()))
    source = """
import os, sys
sys.path[:0] = [os.path.join(os.getcwd(), 'backend'), os.path.join(os.getcwd(), 'backend', 'tests')]
from sqlalchemy import create_engine
from app.scoring.registry import Registry, EvaluationContract
from app.scoring.worker import run_settlement_once
from test_score_review import NonlinearPlugin
engine = create_engine(os.environ['TEST_DATABASE_URL'], connect_args={'options': '-csearch_path=' + os.environ['FLOW_TEST_SCHEMA']})
registry = Registry([NonlinearPlugin()], [EvaluationContract('fake', '1', frozenset({'instruction'}), lambda p: p == {}, False)])
out = run_settlement_once(engine, registry=registry, batch_size=1)
assert len(out['completed']) == 1 and not out['failures']
engine.dispose()
"""
    env = {**os.environ, "FLOW_TEST_SCHEMA": schema}
    proc = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert count(db, FinalScoreResult) == 1
    assert not run_settlement_once(db, registry=f["registry"])["completed"]


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5])
def test_invalid_scanner_batch_size(db, batch_size):
    with pytest.raises(ValueError, match="positive"):
        run_settlement_once(db, batch_size=batch_size)
