from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_evaluation_worker import FakeAdapter, claim
from test_fixed_audio import Synth
from test_paper import draft
from test_publication import TEST_AUDIO_CONFIG
from test_score_settlement import ScorePlugin
from test_submission import deliver, submit
from test_taskbank import CONTENT, write

from app.evaluation.models import EvaluationJob
from app.evaluation.worker import AdapterResult, execute_claim, schedule
from app.media.fixed_audio import AudioStore, prepare_audio
from app.papers.paper import add_entry
from app.papers.publication import publish, select_plugin
from app.scoring.models import (
    AutomaticPaperScore,
    PendingReview,
    ReviewDecision,
    ReviewedPaperScore,
)
from app.scoring.registry import EvaluationContract, Registry
from app.scoring.review import ReviewConflict, record_review
from app.scoring.settlement import settle_session
from app.sessions.assessment_session import (
    AssessmentSession,
    create_session,
    ordered_tasks,
    start_attempt,
)
from app.sessions.close_manifest import close_session

pytest_plugins = ("test_close_manifest",)


class NonlinearPlugin(ScorePlugin):
    def __init__(self):
        super().__init__()
        self.evaluation_requirements["instruction"] = (
            {"source": "fake", "version": "1", "parameters": {}},
        )

    def score_task(self, task_context, raw_evaluations, submission_facts):
        self.calls.append("task")
        return {
            "adjusted": int(
                task_context["reference_answer"] == submission_facts["selected_choice_id"]
            )
        }

    def apply_override(self, task_context, automatic_result, override):
        if type(override) is not int or override < 0:
            raise ValueError("invalid override")
        return {"adjusted": override}

    def score_paper(self, paper_context, task_scores):
        self.calls.append("paper")
        return {
            "total": sum(
                (i + 1) * item["score"]["adjusted"] ** 2 for i, item in enumerate(task_scores)
            )
        }


class ReviewAdapter(FakeAdapter):
    def evaluate(self, inputs, requirement):
        self.calls += 1
        return AdapterResult(
            payload={"metric": 0, "review_recommended": inputs.payload["choice"] == "A"},
            provider="test",
            model="test",
            engine_version="1",
        )


@pytest.fixture
def ready_review(db, bank, tmp_path, request):
    plugin = NonlinearPlugin()
    contract = EvaluationContract(
        "fake",
        "1",
        frozenset({"instruction"}),
        lambda p: p == {},
        lambda payload: payload["review_recommended"],
    )
    registry = Registry([plugin], [contract])
    paper = draft(db, bank)
    tasks = [
        write(
            db,
            bank,
            replace(CONTENT, task_type="instruction", guide_text="guide", reference_answer="A"),
            code=str(i),
        )
        for i in (1, 2)
    ]
    with Session(db) as s, s.begin():
        for i, task in enumerate(tasks):
            add_entry(s, paper_id=paper, expected_version=i, task_id=task.task_id, position=i + 1)
        select_plugin(
            s,
            paper_id=paper,
            expected_version=2,
            plugin_id="test-only",
            version="1",
            registry=registry,
        )
        publish(
            s, paper_id=paper, expected_version=3, registry=registry, audio_config=TEST_AUDIO_CONFIG
        )
    store = AudioStore(tmp_path)
    prepare_audio(db, paper, store, Synth())
    with Session(db) as s, s.begin():
        owner = create_session(
            s,
            paper_id=paper,
            scope=str(uuid4()),
            create_request_id="create",
            review_policy=getattr(request, "param", {"sampling": 0}),
            store=store,
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
    ids = []
    for ctx, choice in zip(contexts, ("A", "B"), strict=True):
        deliver(db, ctx)
        with Session(db) as s, s.begin():
            ids.append(submit(s, ctx, choice=choice).submission_id)
            schedule(s)
    adapter = ReviewAdapter()
    with Session(db) as s:
        jobs = list(s.scalars(select(EvaluationJob.job_id)))
    for job in jobs:
        assert execute_claim(db, claim(db, job, adapter))
    with Session(db) as s, s.begin():
        manifest = close_session(s, session_id=sid, close_request_id="close").manifest_id
        assert settle_session(s, session_id=sid, registry=registry) is None
    return {
        "sid": sid,
        "manifest": manifest,
        "contexts": contexts,
        "submissions": ids,
        "plugin": plugin,
        "registry": registry,
        "adapter": adapter,
    }


def decide(
    s, c, *, operation="op", revision=0, action="CONFIRM", value=None, task=0, teacher="teacher"
):
    return record_review(
        s,
        session_id=c["sid"],
        session_task_id=c["contexts"][task]["session_task_id"],
        submission_id=c["submissions"][task],
        operation_id=operation,
        teacher_id=teacher,
        expected_revision=revision,
        action=action,
        override_value=value,
        registry=c["registry"],
    )


def test_confirm_and_finalize(db, ready_review):
    c = ready_review
    assert c["plugin"].calls == ["task", "task", "paper"]
    with Session(db) as s, s.begin():
        assert s.scalar(select(func.count()).select_from(PendingReview)) == 1
        d = decide(s, c)
        assert decide(s, c).decision_id == d.decision_id
        assert (
            s.scalar(select(ReviewedPaperScore)).result
            == s.scalar(select(AutomaticPaperScore)).result
        )
        result = settle_session(s, session_id=c["sid"], registry=c["registry"])
        assert result.reviewed_paper_score_id is not None
        result_id, decision_id = result.result_id, d.decision_id
    with Session(db) as s, s.begin():
        assert s.get(AssessmentSession, c["sid"]).status == "COMPLETED"
        assert settle_session(s, session_id=c["sid"], registry=Registry()).result_id == result_id
        assert decide(s, c).decision_id == decision_id
    assert c["plugin"].calls == ["task", "task", "paper", "paper"]
    assert c["adapter"].calls == 2
    with pytest.raises(ValueError, match="not open"), Session(db) as s, s.begin():
        decide(s, c, operation="late", revision=1)


def test_overrides_are_versioned_nonlinear_and_idempotent(db, ready_review):
    c = ready_review
    with Session(db) as s, s.begin():
        first = decide(s, c, action="OVERRIDE", value=2)
        assert first.revision == 1
        assert s.scalar(
            select(ReviewedPaperScore).where(ReviewedPaperScore.revision == 1)
        ).result == {"total": 4}
    with pytest.raises(ReviewConflict), Session(db) as s, s.begin():
        decide(s, c, action="OVERRIDE", value=3)
    with Session(db) as s, s.begin():
        second = decide(s, c, operation="second", revision=1, action="OVERRIDE", value=3)
        assert second.revision == 2
        assert s.scalar(
            select(ReviewedPaperScore).where(ReviewedPaperScore.revision == 2)
        ).result == {"total": 9}
        final = settle_session(s, session_id=c["sid"], registry=c["registry"])
        assert s.get(ReviewedPaperScore, final.reviewed_paper_score_id).revision == 2
    assert c["plugin"].calls == ["task", "task", "paper", "paper", "paper"]
    with pytest.raises(DBAPIError), db.begin() as con:
        con.execute(text("UPDATE review_decisions SET action='CONFIRM'"))


def test_review_conflict_and_rollback(db, ready_review):
    c = ready_review
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        decide(s, c)
        raise RuntimeError("rollback")
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 0
        assert s.scalar(select(func.count()).select_from(ReviewedPaperScore)) == 0

    def run(i):
        try:
            with Session(db) as s, s.begin():
                return decide(s, c, operation=f"op{i}", teacher=f"teacher{i}").decision_id
        except ReviewConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(run, range(2)))
    assert sum(item is not None for item in result) == 1
    with pytest.raises(ValueError, match="not selected"), Session(db) as s, s.begin():
        decide(s, c, operation="other", revision=1, task=1)


@pytest.mark.parametrize("ready_review", [{"sampling": 0.25}], indirect=True)
def test_unknown_sampling_remains_closed_after_review(db, ready_review):
    c = ready_review
    with Session(db) as s, s.begin():
        decide(s, c)
        assert settle_session(s, session_id=c["sid"], registry=c["registry"]) is None
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"


def test_uninterpreted_source_cannot_authorize_or_finalize(db, ready_review):
    c = ready_review
    contract = EvaluationContract("fake", "1", frozenset({"instruction"}), lambda p: p == {})
    c["registry"] = Registry([c["plugin"]], [contract])
    with Session(db) as s, s.begin():
        assert settle_session(s, session_id=c["sid"], registry=c["registry"]) is None
    with pytest.raises(ValueError, match="validated source"), Session(db) as s, s.begin():
        decide(s, c)
    with Session(db) as s:
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"


def test_zero_override_and_confirm_removes_previous_override(db, ready_review):
    c = ready_review
    with Session(db) as s, s.begin():
        decide(s, c, action="OVERRIDE", value=0)
        assert s.scalar(
            select(ReviewedPaperScore).where(ReviewedPaperScore.revision == 1)
        ).result == {"total": 0}
    with pytest.raises(ReviewConflict), Session(db) as s, s.begin():
        decide(s, c, action="OVERRIDE", value=False)
    with Session(db) as s, s.begin():
        decide(s, c, operation="confirm", revision=1)
        assert s.scalar(
            select(ReviewedPaperScore).where(ReviewedPaperScore.revision == 2)
        ).result == {"total": 1}
        assert settle_session(s, session_id=c["sid"], registry=c["registry"]) is not None


def test_database_rejects_wrong_submission_and_stale_final(db, ready_review):
    c = ready_review
    with pytest.raises(DBAPIError), db.begin() as con:
        con.execute(
            text("""INSERT INTO review_decisions
          (decision_id, manifest_id, session_task_id, submission_id, revision, operation_id, teacher_id, action)
          VALUES (:id,:manifest,:task,:submission,1,'bad','teacher','CONFIRM')"""),
            {
                "id": uuid4(),
                "manifest": c["manifest"],
                "task": c["contexts"][0]["session_task_id"],
                "submission": c["submissions"][1],
            },
        )
    with Session(db) as s, s.begin():
        decide(s, c)
        stale = s.scalar(
            select(ReviewedPaperScore.score_id).where(ReviewedPaperScore.revision == 1)
        )
    with Session(db) as s, s.begin():
        decide(s, c, operation="second", revision=1)
    with pytest.raises(DBAPIError), db.begin() as con:
        paper_id = con.scalar(select(AutomaticPaperScore.score_id))
        con.execute(
            text("""INSERT INTO final_score_results
          (result_id, session_id, manifest_id, paper_score_id, reviewed_paper_score_id)
          VALUES (:id,:session,:manifest,:paper,:reviewed)"""),
            {
                "id": uuid4(),
                "session": c["sid"],
                "manifest": c["manifest"],
                "paper": paper_id,
                "reviewed": stale,
            },
        )
        con.execute(
            text("UPDATE assessment_sessions SET status='COMPLETED' WHERE session_id=:sid"),
            {"sid": c["sid"]},
        )


def test_override_rejected_when_plugin_has_no_override_contract(db, ready_review):
    c = ready_review
    c["plugin"].apply_override = None
    with pytest.raises(ValueError, match="does not support overrides"), Session(db) as s, s.begin():
        decide(s, c, action="OVERRIDE", value=0)
    with Session(db) as s:
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 0


def test_formal_finalization_rollback_preserves_review(db, ready_review):
    from app.scoring.models import FinalScoreResult

    c = ready_review
    with Session(db) as s, s.begin():
        decide(s, c)
    with pytest.raises(RuntimeError), Session(db) as s, s.begin():
        assert settle_session(s, session_id=c["sid"], registry=c["registry"])
        raise RuntimeError("abort finalization")
    with Session(db) as s:
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"
        assert s.scalar(select(func.count()).select_from(FinalScoreResult)) == 0
        assert s.scalar(select(func.count()).select_from(ReviewDecision)) == 1
        assert s.scalar(select(func.count()).select_from(ReviewedPaperScore)) == 1


def test_migration_preserves_old_automatic_final(db, two_tasks, tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from test_score_settlement import prepare

    from app.scoring.models import FinalScoreResult

    sid, _, plugin, registry = prepare(db, two_tasks, tmp_path, {"sampling": 0})
    with Session(db) as s, s.begin():
        result_id = settle_session(s, session_id=sid, registry=registry).result_id
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0014_score_settlement")
        connection.commit()
        command.upgrade(config, "head")
        connection.commit()
    with Session(db) as s:
        result = s.get(FinalScoreResult, result_id)
        assert result.reviewed_paper_score_id is None
        assert s.get(AssessmentSession, sid).status == "COMPLETED"
        assert s.get(AutomaticPaperScore, result.paper_score_id) is not None
        assert settle_session(s, session_id=sid, registry=Registry()).result_id == result_id
    assert plugin.calls == ["task", "task", "paper"]


def test_legacy_unverified_pending_blocks_finalization(db, ready_review):
    c = ready_review
    with Session(db) as s, s.begin():
        s.add(
            PendingReview(
                review_id=uuid4(),
                manifest_id=c["manifest"],
                session_task_id=c["contexts"][1]["session_task_id"],
                reason="MODEL_RECOMMENDED",
            )
        )
        s.flush()
        decide(s, c)
        assert settle_session(s, session_id=c["sid"], registry=c["registry"]) is None
    with Session(db) as s:
        assert s.get(AssessmentSession, c["sid"]).status == "INPUT_CLOSED"
