"""Authentication contract on real PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.db import get_auth_db, get_db
from app.api.teacher_auth import COOKIE_NAME, password_hasher
from app.auth.models import AuthSession, User
from app.main import app

PASSWORD = "test-password-only"
PASSWORD_HASH = password_hasher.hash(PASSWORD)
ORIGIN = "http://localhost:3000"


@pytest.fixture
def auth_client(db, monkeypatch):
    monkeypatch.setenv("VNEXT_AUTH_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("VNEXT_AUTH_COOKIE_SECURE", "false")
    with Session(db) as s, s.begin():
        for name, role in (("admin", "admin"), ("teacher", "teacher"), ("admin2", "admin")):
            s.add(
                User(
                    user_id=uuid4(),
                    username=name,
                    display_name=name,
                    password_hash=PASSWORD_HASH,
                    role=role,
                    is_active=True,
                )
            )

    def session_override():
        with Session(db) as s:
            yield s

    app.dependency_overrides[get_db] = session_override
    app.dependency_overrides[get_auth_db] = session_override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_auth_db, None)


def login(client, name="admin"):
    return client.post(
        "/auth/login", json={"username": name, "password": PASSWORD}, headers={"Origin": ORIGIN}
    )


def test_login_csrf_and_role(auth_client):
    c = auth_client
    assert c.get("/teacher/banks").status_code == 401
    assert (
        c.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
    )
    assert (
        c.post(
            "/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"Origin": "https://untrusted.invalid"},
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/auth/login",
            json={"username": "missing", "password": "wrong"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 401
    )
    r = login(c, "teacher")
    assert r.status_code == 200
    assert c.cookies.get(COOKIE_NAME) not in r.text
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]
    assert "no-store" in r.headers["cache-control"]
    assert c.get("/teacher/banks").status_code == 200
    assert c.get("/admin/users").status_code == 403
    assert c.get("/teacher/llm-provider").status_code == 403
    assert c.post("/teacher/banks", json={"name": "x"}).status_code == 403
    csrf = r.json()["csrf_token"]
    assert (
        c.post("/teacher/banks", json={"name": "x"}, headers={"X-CSRF-Token": csrf}).status_code
        == 201
    )
    logout = c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 204 and "Max-Age=0" in logout.headers["set-cookie"]
    assert c.post("/auth/logout").status_code == 204
    assert c.get("/auth/me").status_code == 401


def test_change_password_revokes_other_session(auth_client, db):
    c = auth_client
    first = login(c)
    old_cookie = c.cookies.get(COOKIE_NAME)
    first_csrf = first.json()["csrf_token"]
    second = login(c)
    second_cookie = c.cookies.get(COOKIE_NAME)
    assert old_cookie != second_cookie
    assert (
        c.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "new-password"},
            headers={"X-CSRF-Token": second.json()["csrf_token"]},
        ).status_code
        == 204
    )
    c.cookies.set(COOKIE_NAME, old_cookie, domain="testserver.local", path="/")
    assert c.get("/auth/me").status_code == 401
    assert (
        c.post(
            "/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        ).status_code
        == 401
    )
    with Session(db) as s:
        assert len(list(s.scalars(select(AuthSession)))) == 1
    assert first_csrf != second.json()["csrf_token"]


def test_sensitive_validation_does_not_echo(auth_client, caplog):
    secret = "do-not-echo-password"
    r = auth_client.post(
        "/auth/login",
        json={"username": "admin", "password": secret, "unexpected": secret},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 422
    assert secret not in r.text
    assert secret not in caplog.text


def test_competing_administrators_cannot_remove_last_admin(auth_client, db):
    from threading import Barrier

    c = auth_client
    first = login(c, "admin")
    first_cookie = c.cookies.get(COOKIE_NAME)
    second = login(c, "admin2")
    second_cookie = c.cookies.get(COOKIE_NAME)
    a, b = first.json(), second.json()
    barrier = Barrier(2)

    def demote(cookie, csrf, target):
        with TestClient(app) as other:
            other.cookies.set(COOKIE_NAME, cookie, domain="testserver.local", path="/")
            barrier.wait()
            return other.patch(
                f"/admin/users/{target}", json={"role": "teacher"}, headers={"X-CSRF-Token": csrf}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(demote, first_cookie, a["csrf_token"], b["user_id"])
        two = pool.submit(demote, second_cookie, b["csrf_token"], a["user_id"])
        statuses = [one.result(), two.result()]
    assert sorted(statuses) == [200, 401]
    with Session(db) as s:
        assert (
            len(list(s.scalars(select(User).where(User.role == "admin", User.is_active.is_(True)))))
            == 1
        )


def test_admin_reset_and_disable_revoke_sessions(auth_client, db):
    c = auth_client
    teacher_login = login(c, "teacher")
    teacher_cookie = c.cookies.get(COOKIE_NAME)
    teacher_id = teacher_login.json()["user_id"]
    admin_login = login(c)
    headers = {"X-CSRF-Token": admin_login.json()["csrf_token"]}
    assert (
        c.post(
            f"/admin/users/{teacher_id}/reset-password",
            headers=headers,
            json={"new_password": "changed-password"},
        ).status_code
        == 204
    )
    with Session(db) as s:
        assert (
            s.scalar(
                select(AuthSession).where(
                    AuthSession.token_digest
                    == __import__("hashlib").sha256(teacher_cookie.encode()).digest()
                )
            )
            is None
        )
    assert (
        c.patch(
            f"/admin/users/{teacher_id}", headers=headers, json={"is_active": False}
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "teacher", "password": "changed-password"},
        ).status_code
        == 401
    )


def test_wrong_session_csrf_and_expiry(auth_client, db):
    from datetime import UTC, datetime, timedelta

    first = login(auth_client)
    old_csrf = first.json()["csrf_token"]
    second = login(auth_client)
    assert (
        auth_client.post(
            "/teacher/banks", json={"name": "x"}, headers={"X-CSRF-Token": old_csrf}
        ).status_code
        == 403
    )
    assert (
        auth_client.post(
            "/teacher/banks",
            json={"name": "x"},
            headers={"X-CSRF-Token": second.json()["csrf_token"]},
        ).status_code
        == 201
    )
    token = auth_client.cookies.get(COOKIE_NAME)
    from app.api.teacher_auth import token_digest

    with Session(db) as s, s.begin():
        session = s.scalar(
            select(AuthSession).where(AuthSession.token_digest == token_digest(token))
        )
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert auth_client.get("/auth/me").status_code == 401


def test_login_and_password_change_serialize(auth_client, db):
    from threading import Barrier

    from app.api.teacher_auth import token_digest

    current = login(auth_client)
    cookie = auth_client.cookies.get(COOKIE_NAME)
    barrier = Barrier(2)

    def change():
        with TestClient(app) as c:
            c.cookies.set(COOKIE_NAME, cookie, domain="testserver.local", path="/")
            barrier.wait()
            return c.post(
                "/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "new-password"},
                headers={"X-CSRF-Token": current.json()["csrf_token"]},
            ).status_code

    def old_login():
        with TestClient(app) as c:
            barrier.wait()
            result = login(c)
            return result.status_code, c.cookies.get(COOKIE_NAME)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(change)
        b = pool.submit(old_login)
        assert a.result() == 204
        status, candidate = b.result()
    assert status in (200, 401)
    if candidate:
        with Session(db) as s:
            assert (
                s.scalar(
                    select(AuthSession).where(AuthSession.token_digest == token_digest(candidate))
                )
                is None
            )


def test_concurrent_duplicate_username_creation(auth_client):
    from threading import Barrier

    current = login(auth_client)
    cookie = auth_client.cookies.get(COOKIE_NAME)
    csrf = current.json()["csrf_token"]
    barrier = Barrier(2)

    def create():
        with TestClient(app) as c:
            c.cookies.set(COOKIE_NAME, cookie, domain="testserver.local", path="/")
            barrier.wait()
            return c.post(
                "/admin/users",
                headers={"X-CSRF-Token": csrf},
                json={"username": "New.User", "display_name": "New", "password": PASSWORD},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(create), pool.submit(create)
        assert sorted([a.result(), b.result()]) == [201, 409]


def test_initial_admin_interactive_creation(db, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "create_admin.py"
    spec = importlib.util.spec_from_file_location("vnext_create_admin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "create_database_engine", lambda: db)
    monkeypatch.setenv("AUTH_TEST_DATABASE", "postgresql+psycopg://test-placeholder")
    monkeypatch.setattr(sys, "argv", [str(path), "--database-env", "AUTH_TEST_DATABASE"])
    answers = iter([" New.Admin ", " New Admin "])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: PASSWORD)
    assert module.main() == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    with Session(db) as s:
        user = s.scalar(select(User).where(User.username == "new.admin"))
        assert user.role == "admin" and user.is_active
        assert password_hasher.verify(PASSWORD, user.password_hash)
    with pytest.raises(ValueError, match="already exists"):
        module.create_admin("new.admin", "New", PASSWORD)


def test_login_and_disabling_account_serialize(auth_client, db):
    from threading import Barrier

    from app.api.teacher_auth import token_digest

    teacher = login(auth_client, "teacher")
    target = teacher.json()["user_id"]
    admin = login(auth_client)
    cookie = auth_client.cookies.get(COOKIE_NAME)
    barrier = Barrier(2)

    def disable():
        with TestClient(app) as c:
            c.cookies.set(COOKIE_NAME, cookie, domain="testserver.local", path="/")
            barrier.wait()
            return c.patch(
                f"/admin/users/{target}",
                json={"is_active": False},
                headers={"X-CSRF-Token": admin.json()["csrf_token"]},
            ).status_code

    def race_login():
        with TestClient(app) as c:
            barrier.wait()
            result = login(c, "teacher")
            return result.status_code, c.cookies.get(COOKIE_NAME)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(disable), pool.submit(race_login)
        assert a.result() == 200
        status, candidate = b.result()
    assert status in (200, 401)
    if candidate:
        with Session(db) as s:
            assert (
                s.scalar(
                    select(AuthSession).where(AuthSession.token_digest == token_digest(candidate))
                )
                is None
            )


def test_secure_cookie_default(auth_client, monkeypatch):
    monkeypatch.delenv("VNEXT_AUTH_COOKIE_SECURE")
    result = login(auth_client)
    assert result.status_code == 200
    assert "Secure" in result.headers["set-cookie"]


def test_teacher_validation_hides_input_and_child_auth_is_independent(auth_client):
    result = login(auth_client)
    csrf = result.json()["csrf_token"]
    secret = "private-validation-value"
    response = auth_client.post(
        "/teacher/banks", headers={"X-CSRF-Token": csrf}, json={"name": {"password": secret}}
    )
    assert response.status_code == 422 and secret not in response.text
    assert auth_client.get(f"/assessment/sessions/{uuid4()}").status_code == 401
    auth_client.cookies.clear()
    assert (
        auth_client.get(
            "/teacher/banks", headers={"Authorization": "Bearer as1_" + "a" * 43}
        ).status_code
        == 401
    )


def test_role_change_revokes_target_sessions(auth_client):
    teacher = login(auth_client, "teacher")
    target = teacher.json()["user_id"]
    old_cookie = auth_client.cookies.get(COOKIE_NAME)
    admin = login(auth_client)
    response = auth_client.patch(
        f"/admin/users/{target}",
        json={"role": "admin"},
        headers={"X-CSRF-Token": admin.json()["csrf_token"]},
    )
    assert response.status_code == 200 and response.json()["role"] == "admin"
    auth_client.cookies.set(COOKIE_NAME, old_cookie, domain="testserver.local", path="/")
    assert auth_client.get("/auth/me").status_code == 401


def test_unknown_user_and_wrong_password_have_same_response(auth_client):
    wrong = auth_client.post(
        "/auth/login", headers={"Origin": ORIGIN}, json={"username": "admin", "password": "wrong"}
    )
    missing = auth_client.post(
        "/auth/login", headers={"Origin": ORIGIN}, json={"username": "missing", "password": "wrong"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


def test_logout_only_revokes_current_device(auth_client):
    first = login(auth_client)
    first_cookie = auth_client.cookies.get(COOKIE_NAME)
    second = login(auth_client)
    assert first_cookie != auth_client.cookies.get(COOKIE_NAME)
    assert (
        auth_client.post(
            "/auth/logout", headers={"X-CSRF-Token": first.json()["csrf_token"]}
        ).status_code
        == 403
    )
    assert (
        auth_client.post(
            "/auth/logout", headers={"X-CSRF-Token": second.json()["csrf_token"]}
        ).status_code
        == 204
    )
    auth_client.cookies.set(COOKIE_NAME, first_cookie, domain="testserver.local", path="/")
    assert auth_client.get("/auth/me").status_code == 200
