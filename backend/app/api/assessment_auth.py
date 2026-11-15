"""Session-scoped authentication reusable by assessment routes."""

import hashlib
import re
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.teacher_api import get_db
from app.api.teacher_auth import bearer
from app.sessions.credentials import SessionCredential

TOKEN_PATTERN = re.compile(r"as1_[A-Za-z0-9_-]{43}\Z")
credential_dependency = Depends(bearer)
db_dependency = Depends(get_db)


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
            SessionCredential.session_id == session_id, SessionCredential.token_digest == digest
        )
    )
    if found is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    return session_id


def require_session_access(
    session_id: UUID,
    credentials: HTTPAuthorizationCredentials | None = credential_dependency,
    db: Session = db_dependency,
) -> UUID:
    """Reusable session authorization for future assessment actions."""
    return _check_access(session_id, credentials, db)


def assessment_read_db(db: Session = db_dependency) -> Session:
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    return db


read_db_dependency = Depends(assessment_read_db)


def require_read_session_access(
    session_id: UUID,
    credentials: HTTPAuthorizationCredentials | None = credential_dependency,
    db: Session = read_db_dependency,
) -> UUID:
    return _check_access(session_id, credentials, db)
