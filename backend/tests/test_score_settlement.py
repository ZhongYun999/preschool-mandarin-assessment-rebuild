from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

pytest_plugins = ("test_close_manifest",)
from test_publication import TestPlugin
from test_submission import deliver, submit

from app.evaluation.models import EvaluationJob
from app.media.fixed_audio import AudioStore
from app.scoring.models import AutomaticPaperScore, AutomaticTaskScore, FinalScoreResult
from app.scoring.registry import Registry
from app.scoring.settlement import settle_session
from app.sessions.assessment_session import (
    AssessmentSession,
    create_session,
    ordered_tasks,
    start_attempt,
)
from app.sessions.close_manifest import close_session


class ScorePlugin(TestPlugin):
    def __init__(self):
        super().__init__()
        self.evaluation_requirements["instruction"] = ()
        self.calls = []

    def score_task(self, task_context, raw_evaluations, submission_facts):
        self.calls.append("task")
        return {
            "score": int(task_context["reference_answer"] == submission_facts["selected_choice_id"])
        }

    def score_paper(self, paper_context, task_scores):
        self.calls.append("paper")
        return {"total": sum(item["score"]["score"] for item in task_scores)}


def prepare(db, two_tasks, tmp_path, policy):
    plugin = ScorePlugin()
    registry = Registry([plugin])
    with Session(db) as s, s.begin():
        paper_id = s.get(AssessmentSession, two_tasks[0]["session_id"]).paper_id
        owner = create_session(
            s,
            paper_id=paper_id,
            scope=str(uuid4()),
            create_request_id="create",
            review_policy=policy,
            store=AudioStore(tmp_path),
            registry=registry,
        )
        sid = owner.session_id
        contexts = []
        for task in ordered_tasks(s, sid):
            attempt = start_attempt(s, session_id=sid, session_task_id=task.session_task_id)
            contexts.append(
                {
                    "session_id": sid,
                    "session_task_id": task.session_task_id,
                    "attempt_id": attempt.attempt_id,
                }
            )
    for ctx in contexts:
        deliver(db, ctx)
        with Session(db) as s, s.begin():
            submit(s, ctx, choice="A")
    with Session(db) as s, s.begin():
        manifest_id = close_session(s, session_id=sid, close_request_id="close").manifest_id
    return sid, manifest_id, plugin, registry


def test_two_stage_zero_and_idempotency(db, two_tasks, tmp_path):
    sid, mid, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    with Session(db) as s, s.begin():
        result = settle_session(s, session_id=sid, registry=registry)
        rid = result.result_id
    assert plugin.calls == ["task", "task", "paper"]
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=Registry()).result_id == rid
        assert s.get(AssessmentSession, sid).status == "COMPLETED"
        assert s.scalar(
            select(AutomaticPaperScore).where(AutomaticPaperScore.manifest_id == mid)
        ).result == {"total": 1}
        scores = s.scalars(
            select(AutomaticTaskScore).where(AutomaticTaskScore.manifest_id == mid)
        ).all()
        assert sorted(score.result["score"] for score in scores) == [0, 1]
        assert all(score.raw_evaluation_ids == [] for score in scores)
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 0
    assert plugin.calls == ["task", "task", "paper"]
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(
            text("UPDATE automatic_paper_scores SET result='{}' WHERE manifest_id=:id"), {"id": mid}
        )


def test_unknown_policy_and_unavailable_plugin(db, two_tasks, tmp_path):
    sid, mid, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0.25})
    with pytest.raises(ValueError, match="unavailable"), Session(db) as s, s.begin():
        settle_session(s, session_id=sid, registry=Registry())
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry) is None
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry) is None
        assert s.get(AssessmentSession, sid).status == "INPUT_CLOSED"
        assert s.scalar(select(AutomaticPaperScore).where(AutomaticPaperScore.manifest_id == mid))
    assert plugin.calls == ["task", "task", "paper"]


def test_concurrent_settlement_and_rollback(db, two_tasks, tmp_path):
    sid, _mid, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        settle_session(s, session_id=sid, registry=registry)
        raise RuntimeError("rollback")
    with Session(db) as s:
        assert s.get(AssessmentSession, sid).status == "INPUT_CLOSED"
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 0
        assert s.scalar(select(func.count()).select_from(AutomaticTaskScore)) == 0
    plugin.calls.clear()

    def run():
        with Session(db) as s, s.begin():
            return settle_session(s, session_id=sid, registry=registry).result_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    assert plugin.calls == ["task", "task", "paper"]
    with pytest.raises(DBAPIError), db.begin() as c:
        c.execute(
            text("UPDATE assessment_sessions SET status='COMPLETED' WHERE session_id=:id"),
            {"id": two_tasks[0]["session_id"]},
        )


def test_zero_entry_paper_uses_plugin(db, bank, tmp_path):
    from test_paper import draft
    from test_publication import TEST_AUDIO_CONFIG

    from app.papers.publication import publish, select_plugin

    plugin = ScorePlugin()
    registry = Registry([plugin])
    paper = draft(db, bank)
    with Session(db) as s, s.begin():
        select_plugin(
            s,
            paper_id=paper,
            expected_version=0,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        publish(
            s, paper_id=paper, expected_version=1, registry=registry, audio_config=TEST_AUDIO_CONFIG
        )
        owner = create_session(
            s,
            paper_id=paper,
            scope="zero",
            create_request_id="zero",
            review_policy={"sampling": 0},
            store=AudioStore(tmp_path),
            registry=registry,
        )
        sid = owner.session_id
        close_session(s, session_id=sid, close_request_id="close")
    with Session(db) as s, s.begin():
        final = settle_session(s, session_id=sid, registry=registry)
        assert s.get(AutomaticPaperScore, final.paper_score_id).result == {"total": 0}
    assert plugin.calls == ["paper"]


def test_required_evidence_and_model_review(db, context, tmp_path):
    from test_evaluation_worker import FakeAdapter, claim

    from app.evaluation.models import RawEvaluation
    from app.evaluation.worker import AdapterResult, execute_claim, schedule
    from app.scoring.models import PendingReview
    from app.scoring.registry import EvaluationContract
    from app.sessions.assessment_session import redo_attempt

    class RecommendingAdapter(FakeAdapter):
        def evaluate(self, inputs, requirement):
            return AdapterResult(
                payload={"metric": 0, "review_recommended": True},
                provider="test",
                model="test",
                engine_version="1",
            )

    plugin = ScorePlugin()
    plugin.evaluation_requirements["instruction"] = (
        {"source": "fake", "version": "1", "parameters": {}},
    )
    registry = Registry(
        [plugin],
        [
            EvaluationContract(
                "fake",
                "1",
                frozenset({"instruction"}),
                lambda p: p == {},
                lambda payload: payload.get("review_recommended") is True,
            )
        ],
    )
    with Session(db) as s, s.begin():
        paper_id = s.get(AssessmentSession, context["session_id"]).paper_id
        owner = create_session(
            s,
            paper_id=paper_id,
            scope="review",
            create_request_id="review",
            review_policy={"sampling": 0},
            store=AudioStore(tmp_path),
            registry=registry,
        )
        task = ordered_tasks(s, owner.session_id)[0]
        attempt = start_attempt(
            s, session_id=owner.session_id, session_task_id=task.session_task_id
        )
        context = {
            "session_id": owner.session_id,
            "session_task_id": task.session_task_id,
            "attempt_id": attempt.attempt_id,
        }
    deliver(db, context)
    with Session(db) as s, s.begin():
        old_id = submit(s, context).submission_id
        schedule(s)
        old_job = s.scalar(select(EvaluationJob.job_id))
    adapter = RecommendingAdapter()
    assert execute_claim(db, claim(db, old_job, adapter))
    with Session(db) as s, s.begin():
        attempt = redo_attempt(
            s,
            session_id=context["session_id"],
            session_task_id=context["session_task_id"],
            redo_request_id="redo",
        )
        new_context = {**context, "attempt_id": attempt.attempt_id}
    deliver(db, new_context)
    with Session(db) as s, s.begin():
        new_id = submit(s, new_context, request="new").submission_id
        schedule(s)
        close_session(s, session_id=context["session_id"], close_request_id="close")
        new_job = s.scalar(
            select(EvaluationJob.job_id).where(EvaluationJob.submission_id == new_id)
        )
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=context["session_id"], registry=registry) is None
        assert not plugin.calls
    assert execute_claim(db, claim(db, new_job, adapter))
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=context["session_id"], registry=registry) is None
        score = s.scalar(select(AutomaticTaskScore))
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.submission_id == new_id))
        assert score.submission_id == new_id != old_id
        assert score.raw_evaluation_ids == [str(raw.evaluation_id)]
        assert s.scalar(select(PendingReview)).reason == "MODEL_RECOMMENDED"
        assert s.get(AssessmentSession, context["session_id"]).status == "INPUT_CLOSED"


@pytest.mark.parametrize("mode", ["failure", "invalid", "unknown"])
def test_failed_evidence_never_scores(db, context, mode):
    from test_evaluation_worker import FakeAdapter, claim

    from app.evaluation.worker import execute_claim, schedule
    from app.scoring.registry import EvaluationContract

    plugin = ScorePlugin()
    plugin.evaluation_requirements["instruction"] = (
        {"source": "fake", "version": "1", "parameters": {}},
    )
    registry = Registry(
        [plugin], [EvaluationContract("fake", "1", frozenset({"instruction"}), lambda p: p == {})]
    )
    deliver(db, context)
    with Session(db) as s, s.begin():
        submit(s, context)
        schedule(s)
        job = s.scalar(select(EvaluationJob.job_id))
        close_session(s, session_id=context["session_id"], close_request_id="close")
    adapter = FakeAdapter(mode)
    execute_claim(db, claim(db, job, adapter))
    with Session(db) as s, s.begin():
        assert (
            s.get(EvaluationJob, job).status
            == {"failure": "FAILED", "invalid": "INVALID", "unknown": "UNKNOWN"}[mode]
        )
        assert settle_session(s, session_id=context["session_id"], registry=registry) is None
        assert s.scalar(select(func.count()).select_from(AutomaticTaskScore)) == 0
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 0
    assert not plugin.calls


def test_selected_plugin_version_only(db, two_tasks, tmp_path):
    sid, _, plugin, _ = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    other = ScorePlugin()
    other.implementation_version = "2"
    registry = Registry([other, plugin])
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=sid, registry=registry)
    assert plugin.calls == ["task", "task", "paper"]
    assert other.calls == []
