"""Cookie authentication and session-bound CSRF for teacher endpoints."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.db import get_auth_db
from app.auth.models import AuthSession, User

COOKIE_NAME = "vnext_teacher_session"
password_hasher = PasswordHash.recommended()
dummy_hash = password_hasher.hash("vnext-invalid-login-dummy")
_username = re.compile(r"[a-z0-9._-]{1,64}\Z")


def normalize_username(value: str) -> str:
    value = value.strip().lower()
    if not _username.fullmatch(value):
        raise ValueError("Invalid username")
    return value


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"vnext-csrf-v1", hashlib.sha256).hexdigest()


def secure_cookie() -> bool:
    value = os.environ.get("VNEXT_AUTH_COOKIE_SECURE", "true").lower()
    if value not in ("true", "false"):
        raise RuntimeError("Invalid VNEXT_AUTH_COOKIE_SECURE")
    return value == "true"


def allowed_origin(request: Request) -> None:
    allowed = {
        v.strip() for v in os.environ.get("VNEXT_AUTH_ALLOWED_ORIGINS", "").split(",") if v.strip()
    }
    if request.headers.get("origin") not in allowed:
        raise HTTPException(status_code=403, detail="Origin not allowed")


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    session_id: UUID
    username: str
    display_name: str
    role: str
    csrf: str


auth_db = Depends(get_auth_db)


def get_current_user(request: Request, db: Session = auth_db) -> Identity:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 256:
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = db.execute(
        select(User, AuthSession)
        .join(AuthSession, AuthSession.user_id == User.user_id)
        .where(
            AuthSession.token_digest == token_digest(token),
            AuthSession.expires_at > datetime.now(UTC),
            User.is_active.is_(True),
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user, auth_session = row
    csrf = csrf_token(token)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf.encode(), supplied.encode()):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
    return Identity(
        user.user_id, auth_session.session_id, user.username, user.display_name, user.role, csrf
    )


current_user_dependency = Depends(get_current_user)


def require_teacher(identity: Identity = current_user_dependency) -> Identity:
    return identity


def require_admin(identity: Identity = current_user_dependency) -> Identity:
    if identity.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return identity


require_scoring_access = require_teacher
require_review_teacher = require_teacher
