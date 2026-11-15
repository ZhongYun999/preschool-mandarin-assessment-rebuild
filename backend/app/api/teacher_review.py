"""Authenticated review decisions using the existing scoring transaction."""

import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.teacher_api import get_db
from app.api.teacher_auth import require_review_teacher
from app.scoring.models import ReviewedPaperScore
from app.scoring.registry import PRODUCTION_REGISTRY, Registry, ScoringUnavailable
from app.scoring.review import InvalidOverride, ReviewConflict, record_review
from app.scoring.settlement import settle_session
from app.sessions.assessment_session import AssessmentSession, SessionTask

router = APIRouter(prefix="/teacher")


def get_scoring_registry() -> Registry:
    return PRODUCTION_REGISTRY


db_dependency = Depends(get_db)
teacher_dependency = Depends(require_review_teacher)
registry_dependency = Depends(get_scoring_registry)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: UUID
    operation_id: str = Field(min_length=1, strict=True)
    expected_revision: StrictInt = Field(ge=0)
    action: Literal["CONFIRM", "OVERRIDE"]
    override_value: Any = None

    @model_validator(mode="after")
    def validate_action(self):
        if not self.operation_id.strip():
            raise ValueError("Operation identity is required")
        if (self.action == "CONFIRM" and self.override_value is not None) or (
            self.action == "OVERRIDE" and self.override_value is None
        ):
            raise ValueError("Action and override do not match")
        try:
            json.dumps(self.override_value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("Override must be finite JSON") from exc
        return self


class ReviewResponse(BaseModel):
    decision_id: UUID
    revision: int
    session_id: UUID
    session_task_id: UUID
    session_status: str
    final_result_created: bool
    final_result_id: UUID | None
    reviewed_paper_score_id: UUID | None


_CONFLICTS = frozenset(
    {
        "Session has no manifest",
        "Session is not open for review",
        "Submission is not selected for review",
        "Automatic paper score is not ready",
        "Automatic task score is not ready",
        "Automatic task score set is incomplete",
        "Evaluation evidence identity does not match frozen input",
        "Review evidence is not ready or does not match",
        "Review selection has no validated source recommendation or sampling",
        "Session is not eligible for settlement",
    }
)


@router.post(
    "/sessions/{session_id}/reviews/{session_task_id}/decisions", response_model=ReviewResponse
)
def submit_review(
    session_id: UUID,
    session_task_id: UUID,
    body: ReviewRequest,
    teacher_id: str = teacher_dependency,
    db: Session = db_dependency,
    registry: Registry = registry_dependency,
):
    try:
        with db.begin():
            owner = db.get(AssessmentSession, session_id)
            if owner is None:
                raise HTTPException(status_code=404, detail="Session not found")
            task = db.scalar(
                select(SessionTask.session_task_id).where(
                    SessionTask.session_id == session_id,
                    SessionTask.session_task_id == session_task_id,
                )
            )
            if task is None:
                raise HTTPException(status_code=404, detail="Session task not found")
            decision = record_review(
                db,
                session_id=session_id,
                session_task_id=session_task_id,
                submission_id=body.submission_id,
                operation_id=body.operation_id,
                teacher_id=teacher_id,
                expected_revision=body.expected_revision,
                action=body.action,
                override_value=body.override_value,
                registry=registry,
            )
            final = settle_session(db, session_id=session_id, registry=registry)
            snapshot = db.scalar(
                select(ReviewedPaperScore)
                .where(ReviewedPaperScore.manifest_id == decision.manifest_id)
                .order_by(ReviewedPaperScore.revision.desc())
                .limit(1)
            )
            response = ReviewResponse(
                decision_id=decision.decision_id,
                revision=decision.revision,
                session_id=session_id,
                session_task_id=session_task_id,
                session_status=owner.status,
                final_result_created=final is not None,
                final_result_id=final.result_id if final else None,
                reviewed_paper_score_id=snapshot.score_id if snapshot else None,
            )
        return response
    except ReviewConflict as exc:
        raise HTTPException(
            status_code=409, detail="Review revision or operation conflict"
        ) from exc
    except InvalidOverride as exc:
        raise HTTPException(status_code=422, detail="Override rejected by scoring plugin") from exc
    except ScoringUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Frozen scoring capability unavailable"
        ) from exc
    except ValueError as exc:
        if str(exc) in _CONFLICTS:
            raise HTTPException(status_code=409, detail="Review is not eligible") from exc
        raise
