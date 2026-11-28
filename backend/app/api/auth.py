"""Teacher login and administrator account management."""

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.db import get_db
from app.api.teacher_auth import (
    COOKIE_NAME,
    Identity,
    allowed_origin,
    csrf_token,
    dummy_hash,
    get_current_user,
    normalize_username,
    password_hasher,
    require_admin,
    secure_cookie,
    token_digest,
)
from app.auth.models import AuthSession, User


class SafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": "Invalid request"})
            response.headers["Cache-Control"] = "no-store"
            return response

        return wrapped


router = APIRouter(route_class=SafeRoute)
db_dep = Depends(get_db)
current_dep = Depends(get_current_user)
admin_dep = Depends(require_admin)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class ChangePassword(Strict):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ResetPassword(Strict):
    new_password: str = Field(min_length=8, max_length=128)


class CreateUser(Strict):
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["teacher", "admin"] = "teacher"

    @field_validator("display_name")
    @classmethod
    def clean_display(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Invalid display name")
        return value


class UpdateUser(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role: Literal["teacher", "admin"] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_change(self):
        if self.display_name is not None:
            self.display_name = self.display_name.strip()
            if not self.display_name:
                raise ValueError("Invalid display name")
        if self.display_name is None and self.role is None and self.is_active is None:
            raise ValueError("No changes")
        return self


def project(user):
    return {
        "user_id": str(user.user_id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
    }


def _lock_management(db):
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('vnext_auth_admin_management'))"))


def _confirm(db, identity, admin=False):
    user = db.scalar(select(User).where(User.user_id == identity.user_id).with_for_update())
    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.session_id == identity.session_id, AuthSession.user_id == identity.user_id)
        .with_for_update()
    )
    if (
        user is None
        or not user.is_active
        or session is None
        or session.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(401, "Unauthorized")
    if admin and user.role != "admin":
        raise HTTPException(403, "Administrator required")
    return user


@router.post("/auth/login")
def login(body: Login, request: Request, response: Response, db: Session = db_dep):
    allowed_origin(request)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON required")
    try:
        username = normalize_username(body.username)
    except ValueError:
        username = ""
    with db.begin():
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        valid = password_hasher.verify(body.password, user.password_hash if user else dummy_hash)
        if not user or not valid or not user.is_active:
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32)
        try:
            ttl = int(os.environ.get("VNEXT_AUTH_SESSION_HOURS", "24"))
            if ttl <= 0:
                raise ValueError
        except ValueError:
            raise RuntimeError("Invalid VNEXT_AUTH_SESSION_HOURS") from None
        expires = datetime.now(UTC) + timedelta(hours=ttl)
        db.add(
            AuthSession(user_id=user.user_id, token_digest=token_digest(token), expires_at=expires)
        )
        result = project(user)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ttl * 3600,
        expires=expires,
        httponly=True,
        secure=secure_cookie(),
        samesite="lax",
        path="/",
    )
    return {**result, "csrf_token": csrf_token(token)}


@router.get("/auth/me")
def me(identity: Identity = current_dep):
    return {
        "user_id": str(identity.user_id),
        "username": identity.username,
        "display_name": identity.display_name,
        "role": identity.role,
        "csrf_token": identity.csrf,
    }


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = db_dep):
    token = request.cookies.get(COOKIE_NAME)
    if token and len(token) <= 256:
        row = db.scalar(select(AuthSession).where(AuthSession.token_digest == token_digest(token)))
        if row is not None:
            import hmac

            if not hmac.compare_digest(
                csrf_token(token).encode(), request.headers.get("X-CSRF-Token", "").encode()
            ):
                raise HTTPException(403, "CSRF validation failed")
            db.delete(row)
            db.commit()
    response.delete_cookie(
        COOKIE_NAME, path="/", secure=secure_cookie(), httponly=True, samesite="lax"
    )


@router.post("/auth/change-password", status_code=204)
def change_password(body: ChangePassword, identity: Identity = current_dep, db: Session = db_dep):
    with db.begin():
        user = _confirm(db, identity)
        if not password_hasher.verify(body.current_password, user.password_hash):
            raise HTTPException(400, "Invalid current password")
        if password_hasher.verify(body.new_password, user.password_hash):
            raise HTTPException(422, "Password must change")
        user.password_hash = password_hasher.hash(body.new_password)
        db.execute(
            delete(AuthSession).where(
                AuthSession.user_id == user.user_id, AuthSession.session_id != identity.session_id
            )
        )


@router.get("/admin/users")
def list_users(identity: Identity = admin_dep, db: Session = db_dep):
    return [project(user) for user in db.scalars(select(User).order_by(User.username))]


@router.post("/admin/users", status_code=201)
def create_user(body: CreateUser, identity: Identity = admin_dep, db: Session = db_dep):
    try:
        username = normalize_username(body.username)
    except ValueError:
        raise HTTPException(422, "Invalid username") from None
    try:
        with db.begin():
            _lock_management(db)
            _confirm(db, identity, admin=True)
            user = User(
                username=username,
                display_name=body.display_name,
                password_hash=password_hasher.hash(body.password),
                role=body.role,
                is_active=True,
            )
            db.add(user)
            db.flush()
            result = project(user)
    except IntegrityError:
        raise HTTPException(409, "Username already exists") from None
    return result


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: UUID, body: UpdateUser, identity: Identity = admin_dep, db: Session = db_dep
):
    with db.begin():
        _lock_management(db)
        _confirm(db, identity, admin=True)
        user = db.scalar(select(User).where(User.user_id == user_id).with_for_update())
        if user is None:
            raise HTTPException(404, "User not found")
        role = body.role if body.role is not None else user.role
        active = body.is_active if body.is_active is not None else user.is_active
        if user.user_id == identity.user_id and (role != "admin" or not active):
            raise HTTPException(409, "Cannot disable or demote yourself")
        if user.role == "admin" and user.is_active and (role != "admin" or not active):
            count = db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == "admin", User.is_active.is_(True))
            )
            if count <= 1:
                raise HTTPException(409, "At least one active administrator required")
        if role != user.role or active != user.is_active:
            db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
        user.role, user.is_active = role, active
        if body.display_name is not None:
            user.display_name = body.display_name
        db.flush()
        return project(user)


@router.post("/admin/users/{user_id}/reset-password", status_code=204)
def reset_password(
    user_id: UUID, body: ResetPassword, identity: Identity = admin_dep, db: Session = db_dep
):
    with db.begin():
        _lock_management(db)
        _confirm(db, identity, admin=True)
        if user_id == identity.user_id:
            raise HTTPException(409, "Use personal password change")
        user = db.scalar(select(User).where(User.user_id == user_id).with_for_update())
        if user is None:
            raise HTTPException(404, "User not found")
        user.password_hash = password_hasher.hash(body.new_password)
        db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
