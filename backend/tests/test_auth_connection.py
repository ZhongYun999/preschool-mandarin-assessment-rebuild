"""Authentication releases its PostgreSQL connection before business work."""

import pytest
from auth_helpers import AUTH, TEST_TOKEN, USER_ID, prepare_admin
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.db import get_auth_db, get_db
from app.api.teacher_auth import COOKIE_NAME, csrf_token, get_current_user
from app.main import app
from app.taskbank.models import TaskBank


def _request(token, csrf):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/teacher/banks",
            "headers": [
                (b"cookie", f"{COOKIE_NAME}={token}".encode()),
                (b"x-csrf-token", csrf.encode()),
            ],
        }
    )


@pytest.mark.parametrize("case", ["success", "invalid_token", "invalid_csrf"])
def test_authentication_releases_transaction_on_all_paths(db, case):
    prepare_admin(db)
    token = "invalid-token" if case == "invalid_token" else TEST_TOKEN
    csrf = "invalid-csrf" if case == "invalid_csrf" else csrf_token(token)
    with Session(db) as session:
        if case == "success":
            identity = get_current_user(_request(token, csrf), session)
        else:
            with pytest.raises(HTTPException) as caught:
                get_current_user(_request(token, csrf), session)
            assert caught.value.status_code == (401 if case == "invalid_token" else 403)
        assert not session.in_transaction()
        assert db.pool.checkedout() == 0
    if case == "success":
        assert identity.user_id == USER_ID
        assert identity.role == "admin"
        assert identity.csrf == csrf


def test_single_connection_pool_reaches_business_transaction(db):
    prepare_admin(db)
    with db.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    address = "::1" if db.url.host == "::1" else "127.0.0.1"
    engine = create_engine(
        db.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
        connect_args={"hostaddr": address},
    )

    @event.listens_for(engine, "connect")
    def set_schema(connection, record):
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_auth_db] = override
    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            response = client.put(
                "/teacher/assessment/speaking-policy",
                headers=AUTH,
                json={"minimum_duration_seconds": 3.0},
            )
        assert response.status_code == 200
        assert response.json()["minimum_duration_seconds"] == 3.0
        assert engine.pool.checkedout() == 0
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT minimum_duration_seconds FROM speaking_runtime_policy")
                )
                == 3.0
            )
    finally:
        app.dependency_overrides.pop(get_auth_db, None)
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_authentication_does_not_touch_business_transaction(db):
    prepare_admin(db)
    with Session(db) as business, Session(db) as auth:
        bank = TaskBank(name="independent business transaction")
        business.add(bank)
        business.flush()
        identity = get_current_user(_request(TEST_TOKEN, csrf_token(TEST_TOKEN)), auth)
        assert identity.user_id == USER_ID
        assert not auth.in_transaction()
        assert business.in_transaction()
        bank_id = bank.bank_id
        business.commit()
    with Session(db) as verification:
        assert verification.get(TaskBank, bank_id).name == "independent business transaction"
