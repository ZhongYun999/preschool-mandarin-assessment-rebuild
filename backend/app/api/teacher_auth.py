"""Temporary teacher bearer authentication, replaceable independently of business logic."""

import hashlib
import hmac
import json
import os
import re

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


def _management_matches(credentials: HTTPAuthorizationCredentials | None) -> bool:
    expected = os.environ.get("TEACHER_MANAGEMENT_TOKEN")
    return bool(
        expected
        and expected.strip()
        and credentials
        and hmac.compare_digest(credentials.credentials.encode("utf-8"), expected.encode("utf-8"))
    )


def _review_mapping() -> dict[str, bytes] | None:
    raw = os.environ.get("TEACHER_REVIEW_TOKEN_HASHES")
    if not raw or not raw.strip():
        return None

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate identity")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("Invalid credential mapping")
        mapping = {}
        for teacher_id, digest in parsed.items():
            if (
                not isinstance(teacher_id, str)
                or not teacher_id.strip()
                or teacher_id != teacher_id.strip()
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
            ):
                raise ValueError("Invalid credential mapping")
            teacher_id.encode("utf-8")
            mapping[teacher_id] = bytes.fromhex(digest)
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("Duplicate credential digest")
        management = os.environ.get("TEACHER_MANAGEMENT_TOKEN")
        if management and management.strip():
            admin_hash = hashlib.sha256(management.encode("utf-8")).digest()
            if any(hmac.compare_digest(admin_hash, digest) for digest in mapping.values()):
                raise ValueError("Shared management credential cannot identify a teacher")
        return mapping
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=503, detail="Review authentication unavailable") from exc


def _match_review(
    credentials: HTTPAuthorizationCredentials | None, mapping: dict[str, bytes]
) -> str | None:
    if credentials is None:
        return None
    digest = hashlib.sha256(credentials.credentials.encode("utf-8")).digest()
    teacher_id = None
    for identity, expected in mapping.items():
        if hmac.compare_digest(digest, expected):
            teacher_id = identity
    return teacher_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )


def require_scoring_access(
    credentials: HTTPAuthorizationCredentials | None = bearer_dependency,
) -> None:
    """Shared management or a configured individual may read scoring projections."""
    load_dotenv()
    if _management_matches(credentials):
        return
    mapping = _review_mapping()
    if mapping is not None and _match_review(credentials, mapping) is not None:
        return
    if mapping is None and not os.environ.get("TEACHER_MANAGEMENT_TOKEN", "").strip():
        raise HTTPException(status_code=503, detail="Authentication unavailable")
    raise _unauthorized()


def require_review_teacher(
    credentials: HTTPAuthorizationCredentials | None = bearer_dependency,
) -> str:
    """Resolve the reviewer identity only from configured individual credentials."""
    load_dotenv()
    if _management_matches(credentials):
        raise HTTPException(status_code=403, detail="Individual review credential required")
    mapping = _review_mapping()
    if mapping is None:
        raise HTTPException(status_code=503, detail="Review authentication unavailable")
    teacher_id = _match_review(credentials, mapping)
    if teacher_id is None:
        raise _unauthorized()
    return teacher_id
