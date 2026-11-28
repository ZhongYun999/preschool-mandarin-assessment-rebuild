"""Session-scoped authentication reusable by assessment routes."""

import hashlib
import re
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.teacher_api import get_db
from app.sessions.assessment_session import AssessmentSession
from app.sessions.credentials import SessionCredential

TOKEN_PATTERN = re.compile(r"as1_[A-Za-z0-9_-]{43}\Z")
credential_dependency = Depends(HTTPBearer(auto_error=False))
db_dependency = Depends(get_db)
write_auth_db_dependency = Depends(get_db, use_cache=False)


def _check_access(
    session_id: UUID, credentials: HTTPAuthorizationCredentials | None, db: Session
) -> UUID:
    if credentials is None or not TOKEN_PATTERN.fullmatch(credentials.credentials):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    digest = hashlib.sha256(credentials.credentials.encode("ascii")).digest()
    found = db.scalar(
        select(SessionCredential.credential_id).where(
            SessionCredential.session_id == session_id,
            SessionCredential.token_digest == digest,
            SessionCredential.revoked_at.is_(None),
        )
    )
    if found is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    return found


def require_session_access(
    session_id: UUID,
    credentials: HTTPAuthorizationCredentials | None = credential_dependency,
    db: Session = write_auth_db_dependency,
) -> UUID:
    """Return an ID for later confirmation and release the authentication transaction."""
    credential_id = _check_access(session_id, credentials, db)
    db.rollback()
    return credential_id


def assessment_read_db(db: Session = db_dependency) -> Session:
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    return db


read_db_dependency = Depends(assessment_read_db)


def require_read_session_access(
    session_id: UUID,
    credentials: HTTPAuthorizationCredentials | None = credential_dependency,
    db: Session = read_db_dependency,
) -> UUID:
    _check_access(session_id, credentials, db)
    return session_id


def confirm_write_access(db: Session, session_id: UUID, credential_id: UUID) -> None:
    """Serialize authorization with issuance and revocation in the business transaction."""
    owner = db.scalar(
        select(AssessmentSession.session_id)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
    )
    credential = (
        db.scalar(
            select(SessionCredential.credential_id)
            .where(
                SessionCredential.session_id == session_id,
                SessionCredential.credential_id == credential_id,
                SessionCredential.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if owner is not None
        else None
    )
    if credential is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
