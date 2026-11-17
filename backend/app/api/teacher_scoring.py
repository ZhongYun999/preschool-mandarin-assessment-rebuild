"""Read-only projections of persisted teacher scoring facts."""

import math
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from app.api.teacher_api import get_db
from app.api.teacher_auth import require_scoring_access
from app.evaluation.models import EvaluationJob
from app.papers.paper import EvaluationPlan, Paper, PaperEntry
from app.scoring.models import (
    AutomaticPaperScore,
    AutomaticTaskScore,
    FinalScoreResult,
    PendingReview,
    ReviewDecision,
    ReviewedPaperScore,
)
from app.scoring.review_policy import PolicyUndetermined, sample_task, validate_policy
from app.sessions.assessment_session import AssessmentSession, SessionTask
from app.sessions.close_manifest import CloseManifest, CloseManifestEntry
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision

router = APIRouter(prefix="/teacher", dependencies=[Depends(require_scoring_access)])
db_dependency = Depends(get_db)


class SessionSummary(BaseModel):
    session_id: UUID
    paper_id: UUID
    status: str
    created_at: datetime
    progress: str


class SessionPage(BaseModel):
    items: list[SessionSummary]
    total: int
    limit: int
    offset: int


class SafeResult(BaseModel):
    value: Any | None
    redacted: bool


class ScoreView(BaseModel):
    score_id: UUID
    result: SafeResult


class SourceView(BaseModel):
    index: int
    source: str
    version: str
    status: str
    job_id: UUID | None
    error_code: str | None


class DecisionView(BaseModel):
    decision_id: UUID
    revision: int
    action: str
    override_value: SafeResult | None
    effective_result: SafeResult | None


class TaskView(BaseModel):
    session_task_id: UUID
    paper_entry_id: UUID
    position: int
    task_type: str
    submission_id: UUID | None
    evaluations: list[SourceView]
    automatic_score_status: str
    automatic_score: ScoreView | None
    review_id: UUID | None
    review_reason: str | None
    review_status: str | None
    review_qualification: str | None
    latest_decision: DecisionView | None
    effective_score: SafeResult | None
    history: list[DecisionView]


class PaperScoreView(BaseModel):
    score_id: UUID
    revision: int | None
    result: SafeResult


class ScoringView(BaseModel):
    session_id: UUID
    status: str
    manifest_id: UUID | None
    plugin_id: str | None
    plugin_version: str | None
    progress: str
    blockers: list[str]
    diagnostics: list[str]
    tasks: list[TaskView]
    automatic_paper_score: PaperScoreView | None
    review_preview: PaperScoreView | None
    expected_review_revision: int


class ReviewsView(BaseModel):
    session_id: UUID
    manifest_id: UUID | None
    tasks: list[TaskView]
    preview: PaperScoreView | None
    expected_review_revision: int
    blockers: list[str]
    diagnostics: list[str]


class ResultTask(BaseModel):
    session_task_id: UUID
    paper_entry_id: UUID
    position: int
    task_type: str
    submission_id: UUID
    automatic_score: ScoreView
    effective_score: SafeResult
    review_decision_id: UUID | None
    review_revision: int | None


class ResultView(BaseModel):
    session_id: UUID
    status: str
    ready: bool
    result_id: UUID | None = None
    manifest_id: UUID | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None
    paper_score: PaperScoreView | None = None
    tasks: list[ResultTask] = []


# Plugin outputs have no public projection contract. Only recognized numeric
# score fields are returned; strings and unknown fields remain private.
_SAFE_FIELDS = frozenset(
    {
        "score",
        "total",
        "adjusted",
        "scores",
        "dimensions",
        "dimension_scores",
        "accuracy",
        "fluency",
        "completeness",
        "grammar",
        "semantic",
        "pragmatic",
        "weight",
        "weighted_score",
    }
)
_SAFE_ERRORS = frozenset(
    {
        "SUPERSEDED",
        "EXTERNAL_OUTCOME_UNKNOWN",
        "INVALID_RESULT",
        "LEASE_EXPIRED_OUTCOME_UNKNOWN",
        "TEMPORARY_FAILURE",
        "INPUT_IDENTITY_CONFLICT",
        "ASR_NO_TEXT_NOT_ACCEPTED",
        "EVALUATION_CONTRACT_UNAVAILABLE",
    }
)
_IDENTITY = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _project(value):
    if type(value) in (int, float) and (type(value) is int or math.isfinite(value)):
        return value, False
    if isinstance(value, list):
        parts = [_project(v) for v in value]
        return [v for v, _ in parts], any(redacted for _, redacted in parts)
    if isinstance(value, dict):
        output, redacted = {}, False
        for key, item in value.items():
            if key not in _SAFE_FIELDS:
                redacted = True
                continue
            output[key], hidden = _project(item)
            redacted |= hidden
        return output, redacted
    return None, True


def safe(value):
    projected, redacted = _project(value)
    return SafeResult(value=projected, redacted=redacted)


def score_view(score):
    return None if score is None else ScoreView(score_id=score.score_id, result=safe(score.result))


def paper_view(score):
    return (
        None
        if score is None
        else PaperScoreView(
            score_id=score.score_id,
            revision=score.revision if isinstance(score, ReviewedPaperScore) else None,
            result=safe(score.result),
        )
    )


def decision_view(decision):
    return (
        None
        if decision is None
        else DecisionView(
            decision_id=decision.decision_id,
            revision=decision.revision,
            action=decision.action,
            override_value=safe(decision.override_value)
            if decision.override_value is not None
            else None,
            effective_result=safe(decision.effective_result)
            if decision.effective_result is not None
            else None,
        )
    )


def _readonly_snapshot(db: Session) -> None:
    # Keep a coherent view across the projection queries and prevent writes.
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


def owner_or_404(db, sid):
    owner = db.get(AssessmentSession, sid)
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return owner


@router.get("/sessions", response_model=SessionPage)
def sessions(
    db: Session = db_dependency,
    status: Literal["IN_PROGRESS", "INPUT_CLOSED", "COMPLETED"] | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    _readonly_snapshot(db)
    has_decision = exists(
        select(ReviewDecision.decision_id)
        .where(
            ReviewDecision.manifest_id == PendingReview.manifest_id,
            ReviewDecision.session_task_id == PendingReview.session_task_id,
        )
        .correlate(PendingReview)
    )
    waiting_review = exists(
        select(PendingReview.review_id)
        .join(CloseManifest, CloseManifest.manifest_id == PendingReview.manifest_id)
        .where(CloseManifest.session_id == AssessmentSession.session_id, ~has_decision)
    )
    query = select(
        AssessmentSession,
        exists().where(CloseManifest.session_id == AssessmentSession.session_id),
        exists().where(FinalScoreResult.session_id == AssessmentSession.session_id),
        waiting_review,
    )
    count = select(func.count()).select_from(AssessmentSession)
    if status is not None:
        query, count = (
            query.where(AssessmentSession.status == status),
            count.where(AssessmentSession.status == status),
        )
    rows = db.execute(
        query.order_by(AssessmentSession.created_at.desc(), AssessmentSession.session_id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return SessionPage(
        items=[
            SessionSummary(
                session_id=o.session_id,
                paper_id=o.paper_id,
                status=o.status,
                created_at=o.created_at,
                progress="COMPLETED"
                if done
                else "WAITING_REVIEW"
                if waiting
                else "PROCESSING"
                if closed
                else "IN_PROGRESS",
            )
            for o, closed, done, waiting in rows
        ],
        total=db.scalar(count),
        limit=limit,
        offset=offset,
    )


def _policy_status(policy, entries, types):
    # The persisted plan does not declare every registered plugin task type.
    # Validate syntax and known paper entries; do not claim unknown type keys
    # have been verified against an unavailable production registry.
    extra_types = (
        set(policy.get("task_type_ratios", {}))
        if isinstance(policy, dict) and isinstance(policy.get("task_type_ratios", {}), dict)
        else set()
    )
    try:
        validate_policy(
            policy,
            task_types=types | extra_types,
            paper_entry_ids={str(e.paper_entry_id) for e in entries},
        )
    except (PolicyUndetermined, TypeError):
        return "UNINTERPRETABLE"
    return "UNVERIFIED_TYPES" if extra_types - types else "VALID"


def _snapshot(db, sid):
    owner = owner_or_404(db, sid)
    paper = db.get(Paper, owner.paper_id)
    manifest = db.scalar(select(CloseManifest).where(CloseManifest.session_id == sid))
    rows = db.execute(
        select(SessionTask, PaperEntry, TaskRevision, EvaluationPlan)
        .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
        .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
        .outerjoin(EvaluationPlan, EvaluationPlan.paper_entry_id == PaperEntry.paper_entry_id)
        .where(SessionTask.session_id == sid)
        .order_by(PaperEntry.position, PaperEntry.paper_entry_id)
    ).all()
    mid = manifest.manifest_id if manifest else None
    frozen = (
        {
            x.session_task_id: x
            for x in db.scalars(
                select(CloseManifestEntry).where(CloseManifestEntry.manifest_id == mid)
            )
        }
        if mid
        else {}
    )
    scores = (
        {
            x.session_task_id: x
            for x in db.scalars(
                select(AutomaticTaskScore).where(AutomaticTaskScore.manifest_id == mid)
            )
        }
        if mid
        else {}
    )
    pending = (
        {
            x.session_task_id: x
            for x in db.scalars(select(PendingReview).where(PendingReview.manifest_id == mid))
        }
        if mid
        else {}
    )
    decisions = (
        list(
            db.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.manifest_id == mid)
                .order_by(ReviewDecision.revision)
            )
        )
        if mid
        else []
    )
    histories = {}
    for decision in decisions:
        histories.setdefault(decision.session_task_id, []).append(decision)
    previews = (
        list(
            db.scalars(
                select(ReviewedPaperScore)
                .where(ReviewedPaperScore.manifest_id == mid)
                .order_by(ReviewedPaperScore.revision.desc())
            )
        )
        if mid
        else []
    )
    automatic_paper = (
        db.scalar(select(AutomaticPaperScore).where(AutomaticPaperScore.manifest_id == mid))
        if mid
        else None
    )
    policy_status = _policy_status(
        owner.review_policy, [r[1] for r in rows], {r[2].task_type for r in rows}
    )
    blockers, diagnostics = set(), set()
    if not manifest:
        blockers.add("INPUT_NOT_CLOSED")
    if policy_status == "UNINTERPRETABLE":
        blockers.add("REVIEW_POLICY_UNINTERPRETABLE")
    elif policy_status == "UNVERIFIED_TYPES":
        diagnostics.add("REVIEW_POLICY_UNVERIFIED")
    tasks = []
    for task, entry, revision, plan in rows:
        manifest_entry = frozen.get(task.session_task_id)
        submission_id = manifest_entry.submission_id if manifest_entry else None
        if not manifest and task.current_attempt_id:
            submission_id = db.scalar(
                select(Submission.submission_id).where(
                    Submission.attempt_id == task.current_attempt_id,
                    Submission.session_task_id == task.session_task_id,
                )
            )
        jobs = (
            list(
                db.scalars(
                    select(EvaluationJob).where(
                        EvaluationJob.submission_id == submission_id,
                        EvaluationJob.paper_entry_id == entry.paper_entry_id,
                    )
                )
            )
            if submission_id
            else []
        )
        job_by_index = {j.requirement_index: j for j in jobs}
        sources = []
        if manifest and plan is None:
            blockers.add("EVALUATION_PLAN_MISSING")
        for index, requirement in enumerate(plan.requirements if plan else []):
            job = job_by_index.get(index)
            source = requirement.get("source")
            version = requirement.get("version")
            source = (
                source if isinstance(source, str) and _IDENTITY.fullmatch(source) else "UNAVAILABLE"
            )
            version = (
                version
                if isinstance(version, str) and _IDENTITY.fullmatch(version)
                else "UNAVAILABLE"
            )
            state = (
                "CONTRACT_MISMATCH"
                if job and job.requirement != requirement
                else job.status
                if job
                else "MISSING"
            )
            if manifest and state != "READY":
                blockers.add("EVALUATION_" + state)
            sources.append(
                SourceView(
                    index=index,
                    source=source,
                    version=version,
                    status=state,
                    job_id=job.job_id if job else None,
                    error_code=job.error_code if job and job.error_code in _SAFE_ERRORS else None,
                )
            )
        score = scores.get(task.session_task_id)
        if manifest and score is None:
            blockers.add("AUTOMATIC_TASK_SCORE_PENDING")
        selected = pending.get(task.session_task_id)
        history = histories.get(task.session_task_id, [])
        latest = history[-1] if history else None
        qualification = None
        if selected:
            qualification = (
                "INCONSISTENT_REASON"
                if selected.reason
                not in ("SAMPLED", "MODEL_RECOMMENDED", "SAMPLED+MODEL_RECOMMENDED")
                else "UNVERIFIED_SOURCE"
                if "MODEL_RECOMMENDED" in selected.reason
                else "UNVERIFIED"
            )
            if (
                "MODEL_RECOMMENDED" in selected.reason
                and plan is not None
                and not plan.requirements
            ):
                qualification = "INCONSISTENT_SOURCE"
            if (
                selected.reason in ("SAMPLED", "SAMPLED+MODEL_RECOMMENDED")
                and policy_status == "VALID"
            ):
                try:
                    sampled = sample_task(
                        owner.review_policy,
                        session_id=sid,
                        session_task_id=task.session_task_id,
                        paper_entry_id=entry.paper_entry_id,
                        task_type=revision.task_type,
                        task_types={r[2].task_type for r in rows},
                        paper_entry_ids={str(r[1].paper_entry_id) for r in rows},
                    ).sampled
                    if not sampled:
                        qualification = "INCONSISTENT_SAMPLING"
                    elif (
                        selected.reason == "SAMPLED" and plan is not None and not plan.requirements
                    ):
                        qualification = "VERIFIED_SAMPLING"
                except PolicyUndetermined:
                    qualification = "UNVERIFIED"
            if qualification in (
                "INCONSISTENT_SAMPLING",
                "INCONSISTENT_REASON",
                "INCONSISTENT_SOURCE",
            ):
                blockers.add("REVIEW_QUALIFICATION_INCONSISTENT")
            elif qualification != "VERIFIED_SAMPLING":
                diagnostics.add("REVIEW_QUALIFICATION_UNVERIFIED")
            if latest is None:
                blockers.add("REVIEW_PENDING")
        effective = (
            latest.effective_result
            if latest and latest.action == "OVERRIDE"
            else score.result
            if score
            else None
        )
        tasks.append(
            TaskView(
                session_task_id=task.session_task_id,
                paper_entry_id=entry.paper_entry_id,
                position=entry.position,
                task_type=revision.task_type,
                submission_id=submission_id,
                evaluations=sources,
                automatic_score_status="READY" if score else "NOT_READY",
                automatic_score=score_view(score),
                review_id=selected.review_id if selected else None,
                review_reason=(
                    selected.reason
                    if selected.reason
                    in ("SAMPLED", "MODEL_RECOMMENDED", "SAMPLED+MODEL_RECOMMENDED")
                    else "UNRECOGNIZED"
                )
                if selected
                else None,
                review_status=("REVIEWED" if latest else "PENDING") if selected else None,
                review_qualification=qualification,
                latest_decision=decision_view(latest),
                effective_score=safe(effective) if effective is not None else None,
                history=[decision_view(d) for d in history],
            )
        )
    if manifest and automatic_paper is None:
        blockers.add("AUTOMATIC_PAPER_SCORE_PENDING")
    final = db.scalar(select(FinalScoreResult).where(FinalScoreResult.session_id == sid))
    if owner.status == "COMPLETED" and final is None:
        blockers.add("FINAL_RESULT_MISSING")
    if final and owner.status == "COMPLETED":
        blockers.clear()
    progress = (
        "COMPLETED"
        if final and owner.status == "COMPLETED"
        else "IN_PROGRESS"
        if not manifest
        else "WAITING_REVIEW"
        if "REVIEW_PENDING" in blockers
        else "PROCESSING"
    )
    return (
        owner,
        paper,
        manifest,
        tasks,
        automatic_paper,
        previews,
        decisions,
        sorted(blockers),
        sorted(diagnostics),
        progress,
        final,
    )


@router.get("/sessions/{session_id}/scoring", response_model=ScoringView)
def scoring(session_id: UUID, db: Session = db_dependency):
    _readonly_snapshot(db)
    (
        owner,
        paper,
        manifest,
        tasks,
        automatic,
        previews,
        decisions,
        blockers,
        diagnostics,
        progress,
        _,
    ) = _snapshot(db, session_id)
    return ScoringView(
        session_id=session_id,
        status=owner.status,
        manifest_id=manifest.manifest_id if manifest else None,
        plugin_id=paper.scoring_plugin_id,
        plugin_version=paper.scoring_plugin_version,
        progress=progress,
        blockers=blockers,
        diagnostics=diagnostics,
        tasks=tasks,
        automatic_paper_score=paper_view(automatic),
        review_preview=paper_view(previews[0]) if previews else None,
        expected_review_revision=decisions[-1].revision if decisions else 0,
    )


@router.get("/sessions/{session_id}/reviews", response_model=ReviewsView)
def reviews(session_id: UUID, db: Session = db_dependency):
    _readonly_snapshot(db)
    _, _, manifest, tasks, _, previews, decisions, blockers, diagnostics, _, _ = _snapshot(
        db, session_id
    )
    return ReviewsView(
        session_id=session_id,
        manifest_id=manifest.manifest_id if manifest else None,
        tasks=[t for t in tasks if t.review_reason is not None],
        preview=paper_view(previews[0]) if previews else None,
        expected_review_revision=decisions[-1].revision if decisions else 0,
        blockers=blockers,
        diagnostics=diagnostics,
    )


@router.get("/sessions/{session_id}/result", response_model=ResultView)
def result(session_id: UUID, db: Session = db_dependency):
    _readonly_snapshot(db)
    owner = owner_or_404(db, session_id)
    final = db.scalar(select(FinalScoreResult).where(FinalScoreResult.session_id == session_id))
    if owner.status != "COMPLETED" or final is None:
        return ResultView(session_id=session_id, status=owner.status, ready=False)
    automatic = db.get(AutomaticPaperScore, final.paper_score_id)
    reviewed = (
        db.get(ReviewedPaperScore, final.reviewed_paper_score_id)
        if final.reviewed_paper_score_id
        else None
    )
    paper_score = reviewed or automatic
    references = (
        reviewed.inputs
        if reviewed
        else [
            {"task_score_id": score_id, "review_decision_id": None}
            for score_id in automatic.task_score_ids
        ]
    )
    result_tasks = []
    for ref in references:
        score = db.get(AutomaticTaskScore, UUID(ref["task_score_id"]))
        decision = (
            db.get(ReviewDecision, UUID(ref["review_decision_id"]))
            if ref["review_decision_id"]
            else None
        )
        task = db.get(SessionTask, score.session_task_id)
        entry = db.get(PaperEntry, task.paper_entry_id)
        effective = (
            decision.effective_result
            if decision and decision.action == "OVERRIDE"
            else score.result
        )
        result_tasks.append(
            ResultTask(
                session_task_id=task.session_task_id,
                paper_entry_id=entry.paper_entry_id,
                position=entry.position,
                task_type=db.get(TaskRevision, entry.task_revision_id).task_type,
                submission_id=score.submission_id,
                automatic_score=score_view(score),
                effective_score=safe(effective),
                review_decision_id=decision.decision_id if decision else None,
                review_revision=decision.revision if decision else None,
            )
        )
    return ResultView(
        session_id=session_id,
        status=owner.status,
        ready=True,
        result_id=final.result_id,
        manifest_id=final.manifest_id,
        plugin_id=paper_score.plugin_id,
        plugin_version=paper_score.plugin_version,
        paper_score=paper_view(paper_score),
        tasks=result_tasks,
    )
