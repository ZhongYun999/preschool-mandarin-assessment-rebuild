"""Versioned teacher decisions and same-transaction paper recalculation.

Callers provide an authenticated teacher identity and own the transaction.
"""

import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.papers.paper import EvaluationPlan, Paper, PaperEntry
from app.scoring.models import (
    AutomaticPaperScore,
    AutomaticTaskScore,
    PendingReview,
    ReviewDecision,
    ReviewedPaperScore,
)
from app.scoring.registry import PRODUCTION_REGISTRY, Registry, ScoringUnavailable
from app.scoring.review_policy import PolicyUndetermined, review_reason, sample_task
from app.scoring.settlement import _evidence, _json_result
from app.sessions.assessment_session import AssessmentSession, SessionTask, _identity, _transaction
from app.sessions.close_manifest import CloseManifest, manifest_entries
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision


class ReviewConflict(ValueError):
    """The review revision changed before the requested operation."""


class InvalidOverride(ValueError):
    """The frozen scoring plugin rejected the requested override value."""


def record_review(
    db: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    submission_id: UUID,
    operation_id: str,
    teacher_id: str,
    expected_revision: int,
    action: str,
    override_value=None,
    registry: Registry = PRODUCTION_REGISTRY,
) -> ReviewDecision:
    """Record one decision; plugins may implement apply_override(context, automatic, value).

    That method validates the submitted value and returns the plugin's full task
    score representation. A ValueError means the value was rejected; unexpected
    failures propagate. It must be deterministic and must not call external services.
    """
    _transaction(db)
    _identity(operation_id)
    _identity(teacher_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Expected review revision must be a nonnegative integer")
    if (
        action not in ("CONFIRM", "OVERRIDE")
        or (action == "CONFIRM" and override_value is not None)
        or (action == "OVERRIDE" and override_value is None)
    ):
        raise ValueError("Invalid review action or override")
    value = _json_result(override_value) if override_value is not None else None
    owner = db.scalar(
        select(AssessmentSession)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner is None:
        raise ValueError("Session does not exist")
    manifest = db.scalar(select(CloseManifest).where(CloseManifest.session_id == session_id))
    if manifest is None:
        raise ValueError("Session has no manifest")
    prior = db.scalar(
        select(ReviewDecision).where(
            ReviewDecision.manifest_id == manifest.manifest_id,
            ReviewDecision.operation_id == operation_id,
        )
    )
    if prior is not None:
        if (
            prior.session_task_id,
            prior.submission_id,
            prior.teacher_id,
            prior.action,
            prior.revision - 1,
        ) != (session_task_id, submission_id, teacher_id, action, expected_revision) or json.dumps(
            prior.override_value, sort_keys=True
        ) != json.dumps(value, sort_keys=True):
            raise ReviewConflict("Review operation conflicts with existing request")
        return prior
    if owner.status != "INPUT_CLOSED":
        raise ValueError("Session is not open for review")
    revision = db.scalar(
        select(func.coalesce(func.max(ReviewDecision.revision), 0)).where(
            ReviewDecision.manifest_id == manifest.manifest_id
        )
    )
    if revision != expected_revision:
        raise ReviewConflict("Review revision changed")
    entries = manifest_entries(db, manifest.manifest_id)
    item = next(
        (
            e
            for e in entries
            if e.session_task_id == session_task_id and e.submission_id == submission_id
        ),
        None,
    )
    pending = db.scalar(
        select(PendingReview).where(
            PendingReview.manifest_id == manifest.manifest_id,
            PendingReview.session_task_id == session_task_id,
        )
    )
    if item is None or pending is None:
        raise ValueError("Submission is not selected for review")
    paper = db.get(Paper, owner.paper_id)
    plugin = registry.resolve(paper.scoring_plugin_id, paper.scoring_plugin_version)
    if plugin is None:
        raise ScoringUnavailable("Frozen scoring plugin version is unavailable")
    automatic_paper = db.scalar(
        select(AutomaticPaperScore).where(AutomaticPaperScore.manifest_id == manifest.manifest_id)
    )
    if automatic_paper is None:
        raise ValueError("Automatic paper score is not ready")
    task = db.get(SessionTask, session_task_id)
    entry = db.get(PaperEntry, task.paper_entry_id)
    revision_data = db.get(TaskRevision, entry.task_revision_id)
    plan = db.get(EvaluationPlan, entry.paper_entry_id)
    automatic = db.scalar(
        select(AutomaticTaskScore).where(
            AutomaticTaskScore.manifest_id == manifest.manifest_id,
            AutomaticTaskScore.session_task_id == session_task_id,
        )
    )
    if automatic is None or automatic.submission_id != submission_id:
        raise ValueError("Automatic task score is not ready")
    submission = db.get(Submission, submission_id)
    evidence = _evidence(db, submission, entry, revision_data, plan, registry)
    if evidence is None or automatic.raw_evaluation_ids != [
        str(raw.evaluation_id) for raw in evidence
    ]:
        raise ValueError("Review evidence is not ready or does not match")
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
    entry_ids = {
        str(entry_id)
        for entry_id in db.scalars(
            select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == paper.paper_id)
        )
    }
    try:
        sampled = sample_task(
            owner.review_policy,
            session_id=owner.session_id,
            session_task_id=task.session_task_id,
            paper_entry_id=entry.paper_entry_id,
            task_type=revision_data.task_type,
            task_types=registry.task_types_for(
                paper.scoring_plugin_id, paper.scoring_plugin_version
            ),
            paper_entry_ids=entry_ids,
        ).sampled
    except PolicyUndetermined:
        sampled = False
    reason = review_reason(sampled, recommended)
    if reason is None or pending.reason != reason or (source_unknown and not recommended):
        raise ValueError("Review selection has no validated source recommendation or sampling")
    effective_result = None
    if action == "OVERRIDE":
        apply_override = getattr(plugin, "apply_override", None)
        if not callable(apply_override):
            raise ScoringUnavailable("Frozen scoring plugin does not support overrides")
        try:
            overridden = apply_override(
                {
                    "task_type": revision_data.task_type,
                    "task_revision_id": str(revision_data.task_revision_id),
                    "paper_entry_id": str(entry.paper_entry_id),
                    "position": entry.position,
                },
                json.loads(json.dumps(automatic.result)),
                json.loads(json.dumps(value)),
            )
        except ValueError as exc:
            raise InvalidOverride("Override rejected by scoring plugin") from exc
        effective_result = _json_result(overridden)
    decision = ReviewDecision(
        decision_id=uuid4(),
        manifest_id=manifest.manifest_id,
        session_task_id=session_task_id,
        submission_id=submission_id,
        revision=revision + 1,
        operation_id=operation_id,
        teacher_id=teacher_id,
        action=action,
        override_value=value,
        effective_result=effective_result,
    )
    db.add(decision)
    db.flush()
    decisions = list(
        db.scalars(
            select(ReviewDecision)
            .where(ReviewDecision.manifest_id == manifest.manifest_id)
            .order_by(ReviewDecision.revision.desc())
        )
    )
    latest = {}
    for d in decisions:
        latest.setdefault(d.session_task_id, d)
    inputs, task_scores = [], []
    for e in entries:
        current_task = db.get(SessionTask, e.session_task_id)
        current_entry = db.get(PaperEntry, current_task.paper_entry_id)
        current_revision = db.get(TaskRevision, current_entry.task_revision_id)
        score = db.scalar(
            select(AutomaticTaskScore).where(
                AutomaticTaskScore.manifest_id == manifest.manifest_id,
                AutomaticTaskScore.session_task_id == e.session_task_id,
            )
        )
        current = latest.get(e.session_task_id)
        if score is None or score.submission_id != e.submission_id:
            raise ValueError("Automatic task score set is incomplete")
        effective = (
            current.effective_result if current and current.action == "OVERRIDE" else score.result
        )
        inputs.append(
            {
                "task_score_id": str(score.score_id),
                "review_decision_id": str(current.decision_id) if current else None,
            }
        )
        task_scores.append(
            {
                "paper_entry_id": str(current_entry.paper_entry_id),
                "position": current_entry.position,
                "task_type": current_revision.task_type,
                "score": json.loads(json.dumps(effective)),
            }
        )
    result = _json_result(
        plugin.score_paper(
            {"paper_id": str(paper.paper_id), "name": paper.name, "description": paper.description},
            task_scores,
        )
    )
    db.add(
        ReviewedPaperScore(
            score_id=uuid4(),
            manifest_id=manifest.manifest_id,
            revision=revision + 1,
            plugin_id=paper.scoring_plugin_id,
            plugin_version=paper.scoring_plugin_version,
            inputs=inputs,
            result=result,
        )
    )
    db.flush()
    return decision
