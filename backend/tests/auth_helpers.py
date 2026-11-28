"""Real persisted test accounts and cookies without repeated password hashing."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.api.teacher_auth import COOKIE_NAME, csrf_token, dummy_hash, token_digest
from app.auth.models import AuthSession, User

TEST_TOKEN = "test-only-admin-cookie-token-unchanging"
AUTH = {"Cookie": f"{COOKIE_NAME}={TEST_TOKEN}", "X-CSRF-Token": csrf_token(TEST_TOKEN)}
USER_ID = UUID("30000000-0000-4000-8000-000000000001")


def prepare_admin(engine):
    with Session(engine) as session, session.begin():
        if session.get(User, USER_ID) is None:
            session.add(
                User(
                    user_id=USER_ID,
                    username="test-admin",
                    display_name="Test Admin",
                    password_hash=dummy_hash,
                    role="admin",
                    is_active=True,
                )
            )
            session.flush()
            session.add(
                AuthSession(
                    user_id=USER_ID,
                    token_digest=token_digest(TEST_TOKEN),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
