"""Temporary teacher bearer authentication, replaceable independently of business logic."""

import hmac
import os

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)
bearer_dependency = Depends(bearer)


def require_teacher(credentials: HTTPAuthorizationCredentials | None = bearer_dependency) -> None:
    load_dotenv()
    expected = os.environ.get("TEACHER_MANAGEMENT_TOKEN")
    if not expected or not expected.strip():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="服务不可用")
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权",
            headers={"WWW-Authenticate": "Bearer"},
        )
