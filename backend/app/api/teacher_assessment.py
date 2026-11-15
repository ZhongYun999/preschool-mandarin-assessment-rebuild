"""Teacher-created assessment sessions and explicit additive credential issuance."""

import hashlib
import secrets
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.teacher_api import get_audio_store, get_db
from app.api.teacher_auth import require_teacher
from app.api.teacher_review import get_scoring_registry
from app.media.fixed_audio import AudioStore
from app.papers.paper import Paper, PaperEntry
from app.scoring.registry import Registry
from app.scoring.review_policy import PolicyUndetermined, validate_policy
from app.sessions.assessment_session import AssessmentSession, create_session
from app.sessions.credentials import SessionCredential

router = APIRouter(prefix="/teacher/assessment", dependencies=[Depends(require_teacher)])
db_dependency = Depends(get_db)
audio_dependency = Depends(get_audio_store)
registry_dependency = Depends(get_scoring_registry)
_MANAGEMENT_SCOPE = "teacher-management"


def _same_json_value(left, right):
    # JSON booleans are not numbers, even though Python compares False == 0.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[k], right[k]) for k in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: UUID
    create_request_id: str = Field(strict=True, min_length=1)
    review_policy: dict[str, Any]

    @field_validator("create_request_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Request identity is required")
        return value


class SessionCreated(BaseModel):
    session_id: UUID
    paper_id: UUID
    status: str


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenIssued(BaseModel):
    session_id: UUID
    token: str
    token_type: str = "Bearer"


@router.post("/sessions", response_model=SessionCreated)
def create_assessment(
    body: CreateSessionRequest,
    db: Session = db_dependency,
    store: AudioStore = audio_dependency,
    registry: Registry = registry_dependency,
):
    try:
        with db.begin():
            existing = db.scalar(
                select(AssessmentSession.session_id).where(
                    AssessmentSession.scope == _MANAGEMENT_SCOPE,
                    AssessmentSession.create_request_id == body.create_request_id,
                )
            )
            owner = create_session(
                db,
                paper_id=body.paper_id,
                scope=_MANAGEMENT_SCOPE,
                create_request_id=body.create_request_id,
                review_policy=body.review_policy,
                store=store,
                registry=registry,
            )
            if not _same_json_value(body.review_policy, owner.review_policy):
                raise HTTPException(status_code=409, detail="Session creation request conflicts")
            if existing is None:
                paper = db.get(Paper, body.paper_id)
                if body.review_policy.get("version") != "v1":
                    raise PolicyUndetermined("New sessions require a V1 review policy")
                entry_ids = {
                    str(i)
                    for i in db.scalars(
                        select(PaperEntry.paper_entry_id).where(
                            PaperEntry.paper_id == body.paper_id
                        )
                    )
                }
                validate_policy(
                    body.review_policy,
                    task_types=registry.task_types_for(
                        paper.scoring_plugin_id, paper.scoring_plugin_version
                    ),
                    paper_entry_ids=entry_ids,
                )
            result = SessionCreated(
                session_id=owner.session_id, paper_id=owner.paper_id, status=owner.status
            )
        return result
    except PolicyUndetermined as exc:
        raise HTTPException(status_code=422, detail="Invalid review policy") from exc
    except ValueError as exc:
        if str(exc) in (
            "Scoring plugin is unavailable",
            "Frozen evaluation contract is unavailable or incompatible",
        ):
            raise HTTPException(
                status_code=503, detail="Frozen execution capability unavailable"
            ) from exc
        if str(exc) in (
            "Paper is not published",
            "Paper execution contract or audio is not ready",
            "Create request conflicts with existing session",
        ):
            raise HTTPException(status_code=409, detail="Session creation is not eligible") from exc
        if str(exc) == "Review policy must be a nonempty JSON object":
            raise HTTPException(status_code=422, detail="Invalid review policy") from exc
        raise


@router.post("/sessions/{session_id}/access-token", response_model=TokenIssued)
def issue_access_token(
    session_id: UUID,
    response: Response,
    body: TokenRequest | None = None,
    db: Session = db_dependency,
):
    with db.begin():
        owner = db.scalar(
            select(AssessmentSession)
            .where(AssessmentSession.session_id == session_id)
            .with_for_update()
        )
        if owner is None:
            raise HTTPException(status_code=404, detail="Session not found")
        token = "as1_" + secrets.token_urlsafe(32)
        db.add(
            SessionCredential(
                credential_id=uuid4(),
                session_id=session_id,
                token_digest=hashlib.sha256(token.encode("ascii")).digest(),
            )
        )
        db.flush()
        result = TokenIssued(session_id=session_id, token=token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return result
