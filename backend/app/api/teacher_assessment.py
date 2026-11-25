"""Teacher-created assessment sessions and explicit additive credential issuance."""

import hashlib
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
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
from app.task_types.speaking import SpeakingPolicy

router = APIRouter(prefix="/teacher/assessment", dependencies=[Depends(require_teacher)])
db_dependency = Depends(get_db)
audio_dependency = Depends(get_audio_store)
registry_dependency = Depends(get_scoring_registry)
_MANAGEMENT_SCOPE = "teacher-management"


class SpeakingPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_duration_seconds: float = Field(gt=0, allow_inf_nan=False, strict=True)


@router.put("/speaking-policy")
def set_speaking_policy(body: SpeakingPolicyInput, db: Session = db_dependency):
    with db.begin():
        stmt = insert(SpeakingPolicy).values(
            policy_id=1, minimum_duration_seconds=body.minimum_duration_seconds, revision=1
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SpeakingPolicy.policy_id],
            set_={
                "minimum_duration_seconds": stmt.excluded.minimum_duration_seconds,
                "revision": SpeakingPolicy.revision + 1,
            },
        ).returning(SpeakingPolicy.minimum_duration_seconds, SpeakingPolicy.revision)
        row = db.execute(stmt).one()
        return {"minimum_duration_seconds": row.minimum_duration_seconds, "revision": row.revision}


@router.get("/speaking-policy")
def get_speaking_policy(db: Session = db_dependency):
    policy = db.get(SpeakingPolicy, 1)
    if policy is None:
        raise HTTPException(404, "Speaking policy is not configured")
    return {
        "minimum_duration_seconds": policy.minimum_duration_seconds,
        "revision": policy.revision,
    }


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
    credential_id: UUID
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
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        owner = db.scalar(
            select(AssessmentSession)
            .where(AssessmentSession.session_id == session_id)
            .with_for_update()
        )
        if owner is None:
            raise HTTPException(status_code=404, detail="Session not found")
        token = "as1_" + secrets.token_urlsafe(32)
        credential = SessionCredential(
            credential_id=uuid4(),
            session_id=session_id,
            token_digest=hashlib.sha256(token.encode("ascii")).digest(),
        )
        db.add(credential)
        db.flush()
        result = TokenIssued(
            credential_id=credential.credential_id, session_id=session_id, token=token
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return result


class CredentialMetadata(BaseModel):
    credential_id: UUID
    issued_at: datetime
    revoked_at: datetime | None
    active: bool


def _lock_owner(db: Session, session_id: UUID) -> None:
    owner = db.scalar(
        select(AssessmentSession.session_id)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
    )
    if owner is None:
        raise HTTPException(status_code=404, detail="Session not found")


def _metadata(row: SessionCredential) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=row.credential_id,
        issued_at=row.issued_at,
        revoked_at=row.revoked_at,
        active=row.revoked_at is None,
    )


@router.get("/sessions/{session_id}/access-tokens", response_model=list[CredentialMetadata])
def list_access_tokens(session_id: UUID, response: Response, db: Session = db_dependency):
    if db.get(AssessmentSession, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = db.scalars(
        select(SessionCredential)
        .where(SessionCredential.session_id == session_id)
        .order_by(SessionCredential.issued_at, SessionCredential.credential_id)
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return [_metadata(row) for row in rows]


@router.post(
    "/sessions/{session_id}/access-tokens/{credential_id}/revoke", response_model=CredentialMetadata
)
def revoke_access_token(
    session_id: UUID, credential_id: UUID, response: Response, db: Session = db_dependency
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_owner(db, session_id)
        row = db.scalar(
            select(SessionCredential)
            .where(
                SessionCredential.session_id == session_id,
                SessionCredential.credential_id == credential_id,
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        if row.revoked_at is None:
            row.revoked_at = db.scalar(select(func.clock_timestamp()))
            db.flush()
        result = _metadata(row)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/sessions/{session_id}/access-tokens/revoke-all")
def revoke_all_access_tokens(session_id: UUID, response: Response, db: Session = db_dependency):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_owner(db, session_id)
        ids = db.scalars(
            update(SessionCredential)
            .where(
                SessionCredential.session_id == session_id, SessionCredential.revoked_at.is_(None)
            )
            .values(revoked_at=func.clock_timestamp())
            .returning(SessionCredential.credential_id)
        ).all()
    response.headers["Cache-Control"] = "no-store"
    return {"revoked_count": len(ids)}
