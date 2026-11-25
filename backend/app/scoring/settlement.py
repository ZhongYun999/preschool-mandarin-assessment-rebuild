"""Settle only the immutable inputs selected by a closed session.

The caller owns authorization and the transaction. Scoring plugins must be pure;
external evaluation happens in the evaluation worker before this service runs.
"""

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import _upstream_asr, speaking_source, valid_speaking_consumption
from app.papers.paper import EvaluationPlan, Paper, PaperEntry
from app.scoring.models import (
    AutomaticPaperScore,
    AutomaticTaskScore,
    FinalScoreResult,
    PendingReview,
    ReviewDecision,
    ReviewedPaperScore,
)
from app.scoring.registry import PRODUCTION_REGISTRY, Registry, ScoringUnavailable
from app.scoring.review_policy import (
    PolicyUndetermined,
    review_reason,
    sample_task,
    validate_policy,
)
from app.sessions.assessment_session import AssessmentSession, SessionTask, _transaction
from app.sessions.close_manifest import CloseManifest, SettlementIntent, manifest_entries
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision


def _json_result(value):
    if value is None:
        raise ValueError("Scoring plugin returned no result")
    return json.loads(json.dumps(value, allow_nan=False))


def _evidence(db, submission, entry, revision, plan, registry):
    evidence = []
    source = (
        speaking_source(db, submission, entry, revision)
        if revision.task_type == "speaking"
        else None
    )
    for index, requirement in enumerate(plan.requirements):
        if not registry.supports(revision.task_type, requirement):
            raise ScoringUnavailable("Frozen evaluation contract is unavailable")
        job = db.scalar(
            select(EvaluationJob).where(
                EvaluationJob.submission_id == submission.submission_id,
                EvaluationJob.paper_entry_id == entry.paper_entry_id,
                EvaluationJob.requirement_index == index,
            )
        )
        if job is None or job.status != "READY":
            return None
        raw = db.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == job.job_id, RawEvaluation.status == "READY"
            )
        )
        refs = job.input_refs or {}
        if (
            job.requirement != requirement
            or raw is None
            or raw.submission_id != submission.submission_id
            or raw.input_refs != job.input_refs
            or raw.input_fingerprint != job.input_fingerprint
            or refs.get("submission_id") != str(submission.submission_id)
            or refs.get("paper_entry_id") != str(entry.paper_entry_id)
            or refs.get("task_revision_id") != str(revision.task_revision_id)
            or (
                source is not None
                and (
                    refs.get("speaking_source") != source
                    or not isinstance(refs.get("dependencies"), dict)
                    or not valid_speaking_consumption(
                        registry, requirement, source, refs["dependencies"]
                    )
                )
            )
        ):
            raise ValueError("Evaluation evidence identity does not match frozen input")
        if "depends_on" in requirement:
            if not registry.valid_dependency(revision.task_type, plan.requirements, index):
                raise ScoringUnavailable("Frozen evaluation dependency is unavailable")
            upstream = _upstream_asr(db, job, submission, revision, plan, registry, source)
            dependencies = refs.get("dependencies")
            if (
                upstream is None
                or not isinstance(dependencies, dict)
                or dependencies.get("asr") != upstream[0]
            ):
                raise ValueError("Evaluation dependency identity does not match frozen input")
        evidence.append(raw)
    return evidence


def diagnose_review_policy(
    db: Session, *, session_id: UUID, registry: Registry = PRODUCTION_REGISTRY
) -> str | None:
    """Return why the frozen policy cannot be interpreted, without changing scores."""
    owner = db.get(AssessmentSession, session_id)
    if owner is None:
        raise ValueError("Session does not exist")
    paper = db.get(Paper, owner.paper_id)
    if registry.resolve(paper.scoring_plugin_id, paper.scoring_plugin_version) is None:
        return "Frozen scoring plugin version is unavailable"
    entry_ids = {
        str(entry_id)
        for entry_id in db.scalars(
            select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == paper.paper_id)
        )
    }
    try:
        validate_policy(
            owner.review_policy,
            task_types=registry.task_types_for(
                paper.scoring_plugin_id, paper.scoring_plugin_version
            ),
            paper_entry_ids=entry_ids,
        )
    except PolicyUndetermined as exc:
        return str(exc)
    return None


def settle_session(db: Session, *, session_id: UUID, registry: Registry = PRODUCTION_REGISTRY):
    """Return the formal result, or None while evaluation/review is pending."""
    _transaction(db)
    owner = db.scalar(
        select(AssessmentSession)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner is None:
        raise ValueError("Session does not exist")
    existing = db.scalar(select(FinalScoreResult).where(FinalScoreResult.session_id == session_id))
    if owner.status == "COMPLETED":
        if existing is None:
            raise ValueError("Completed session has no formal result")
        return existing
    if owner.status != "INPUT_CLOSED" or existing is not None:
        raise ValueError("Session is not eligible for settlement")
    manifest = db.scalar(select(CloseManifest).where(CloseManifest.session_id == session_id))
    if manifest is None or db.get(SettlementIntent, manifest.manifest_id) is None:
        raise ValueError("Closed session has no settlement intent")
    paper = db.get(Paper, owner.paper_id)
    plugin = registry.resolve(paper.scoring_plugin_id, paper.scoring_plugin_version)
    if plugin is None:
        raise ScoringUnavailable("Frozen scoring plugin version is unavailable")
    entries = manifest_entries(db, manifest.manifest_id)
    task_types = registry.task_types_for(paper.scoring_plugin_id, paper.scoring_plugin_version)
    entry_ids = set(
        db.scalars(select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == paper.paper_id))
    )
    entry_ids = {str(entry_id) for entry_id in entry_ids}
    try:
        validate_policy(owner.review_policy, task_types=task_types, paper_entry_ids=entry_ids)
        policy_known = True
    except PolicyUndetermined:
        policy_known = False
    scores = []
    review_tasks = {}
    review_unknown = not policy_known
    incomplete = False
    for item in entries:
        task = db.get(SessionTask, item.session_task_id)
        entry = db.get(PaperEntry, task.paper_entry_id)
        revision = db.get(TaskRevision, entry.task_revision_id)
        plan = db.get(EvaluationPlan, entry.paper_entry_id)
        submission = db.get(Submission, item.submission_id)
        if (
            plan is None
            or plan.task_revision_id != revision.task_revision_id
            or (plan.plugin_id, plan.plugin_version)
            != (paper.scoring_plugin_id, paper.scoring_plugin_version)
            or registry.requirements_for(plan.plugin_id, plan.plugin_version, revision.task_type)
            != plan.requirements
        ):
            raise ValueError("Frozen scoring plan is unavailable or inconsistent")
        evidence = _evidence(db, submission, entry, revision, plan, registry)
        if evidence is None:
            incomplete = True
            continue
        sampled = False
        if policy_known:
            sampled = sample_task(
                owner.review_policy,
                session_id=owner.session_id,
                session_task_id=task.session_task_id,
                paper_entry_id=entry.paper_entry_id,
                task_type=revision.task_type,
                task_types=task_types,
                paper_entry_ids=entry_ids,
            ).sampled
        recommended, source_unknown = False, False
        for raw, requirement in zip(evidence, plan.requirements, strict=True):
            recommendation = (
                registry.review_recommendation(requirement, raw.payload)
                if isinstance(raw.payload, dict)
                else None
            )
            if recommendation is None:
                source_unknown = True
            elif recommendation:
                recommended = True
        review_unknown |= source_unknown
        reason = review_reason(sampled, recommended)
        if reason and (not source_unknown or recommended):
            review_tasks[task.session_task_id] = reason
        score = db.scalar(
            select(AutomaticTaskScore).where(
                AutomaticTaskScore.manifest_id == manifest.manifest_id,
                AutomaticTaskScore.session_task_id == task.session_task_id,
            )
        )
        if score is None:
            result = _json_result(
                plugin.score_task(
                    {
                        "paper_entry_id": str(entry.paper_entry_id),
                        "position": entry.position,
                        "task_revision_id": str(revision.task_revision_id),
                        "task_type": revision.task_type,
                        "grade_level": revision.grade_level,
                        "prompt_text": revision.prompt_text,
                        "material_description": revision.material_description,
                        "target_rounds": revision.target_rounds,
                        "reference_answer": revision.reference_answer,
                    },
                    [
                        {
                            "evaluation_id": str(raw.evaluation_id),
                            "payload": raw.payload,
                            "input_refs": raw.input_refs,
                            "requirement": requirement,
                            "provider": raw.provider,
                            "model": raw.model,
                            "engine_version": raw.engine_version,
                        }
                        for raw, requirement in zip(evidence, plan.requirements, strict=True)
                    ],
                    submission.inputs,
                )
            )
            score = AutomaticTaskScore(
                score_id=uuid4(),
                manifest_id=manifest.manifest_id,
                session_task_id=task.session_task_id,
                submission_id=submission.submission_id,
                plugin_id=paper.scoring_plugin_id,
                plugin_version=paper.scoring_plugin_version,
                raw_evaluation_ids=[str(raw.evaluation_id) for raw in evidence],
                result=result,
            )
            db.add(score)
            db.flush()
        scores.append((entry, revision, score))
    if incomplete:
        return None
    paper_score = db.scalar(
        select(AutomaticPaperScore).where(AutomaticPaperScore.manifest_id == manifest.manifest_id)
    )
    if paper_score is None:
        result = _json_result(
            plugin.score_paper(
                {
                    "paper_id": str(paper.paper_id),
                    "name": paper.name,
                    "description": paper.description,
                },
                [
                    {
                        "paper_entry_id": str(entry.paper_entry_id),
                        "position": entry.position,
                        "task_type": revision.task_type,
                        "score": score.result,
                    }
                    for entry, revision, score in scores
                ],
            )
        )
        paper_score = AutomaticPaperScore(
            score_id=uuid4(),
            manifest_id=manifest.manifest_id,
            plugin_id=paper.scoring_plugin_id,
            plugin_version=paper.scoring_plugin_version,
            task_score_ids=[str(score.score_id) for _, _, score in scores],
            result=result,
        )
        db.add(paper_score)
        db.flush()
    for task_id, reason in review_tasks.items():
        existing_review = db.scalar(
            select(PendingReview).where(
                PendingReview.manifest_id == manifest.manifest_id,
                PendingReview.session_task_id == task_id,
            )
        )
        if existing_review is None:
            db.add(
                PendingReview(
                    review_id=uuid4(),
                    manifest_id=manifest.manifest_id,
                    session_task_id=task_id,
                    reason=reason,
                )
            )
        elif existing_review.reason != reason:
            review_unknown = True
    db.flush()
    recorded_review_tasks = set(
        db.scalars(
            select(PendingReview.session_task_id).where(
                PendingReview.manifest_id == manifest.manifest_id
            )
        )
    )
    if recorded_review_tasks - set(review_tasks):
        review_unknown = True
    if review_unknown:
        return None
    reviewed_score = None
    if review_tasks:
        decisions = list(
            db.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.manifest_id == manifest.manifest_id)
                .order_by(ReviewDecision.revision.desc())
            )
        )
        if not set(review_tasks).issubset({d.session_task_id for d in decisions}):
            return None
        reviewed_score = db.scalar(
            select(ReviewedPaperScore).where(
                ReviewedPaperScore.manifest_id == manifest.manifest_id,
                ReviewedPaperScore.revision == decisions[0].revision,
            )
        )
        if reviewed_score is None:
            raise ValueError("Latest review has no paper score snapshot")
    result = FinalScoreResult(
        result_id=uuid4(),
        session_id=session_id,
        manifest_id=manifest.manifest_id,
        paper_score_id=paper_score.score_id,
        reviewed_paper_score_id=reviewed_score.score_id if reviewed_score else None,
    )
    db.add(result)
    owner.status = "COMPLETED"
    db.flush()
    return result
