"""Session-authorized repetition recording endpoints."""

import hashlib
import os
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.assessment_auth import (
    assessment_read_db,
    confirm_write_access,
    require_read_session_access,
    require_session_access,
)
from app.api.assessment_operations import conflict, task_row
from app.api.teacher_api import get_db
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.sessions.assessment_session import AssessmentSession, Attempt
from app.task_types.repetition import (
    cancel_recording,
    save_recording,
    start_recording,
    submit_repetition,
)

router = APIRouter(prefix="/assessment/sessions/{session_id}/tasks/{task_id}")
read_auth = Depends(require_read_session_access)
write_auth = Depends(require_session_access)
read_db = Depends(assessment_read_db)
write_db = Depends(get_db, use_cache=False)
attempt_header = Header(alias="X-Attempt-Id")


def get_recording_store():
    return RecordingStore()


recording_store = Depends(get_recording_store)


class RecordingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    request_id: str = Field(min_length=1, pattern=r"\S")


class RecordingCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


class RepetitionSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    recording_id: UUID
    submit_request_id: str = Field(min_length=1, pattern=r"\S")


def view(row, session_id, task_id):
    return {
        "recording_id": row.recording_id,
        "attempt_id": row.attempt_id,
        "request_id": row.request_id,
        "status": row.status,
        "media_type": row.media_type,
        "content_url": f"/assessment/sessions/{session_id}/tasks/{task_id}/recordings/{row.recording_id}/content"
        if row.status == "SAVED"
        else None,
    }


@router.post("/recordings")
def begin_recording(
    session_id: UUID,
    task_id: UUID,
    body: RecordingStart,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = start_recording(
                db, session_id=session_id, session_task_id=task_id, **body.model_dump()
            )
            return view(row, session_id, task_id)
        except ValueError as exc:
            conflict(exc)


@router.post("/recordings/{recording_id}/cancel")
def cancel(
    session_id: UUID,
    task_id: UUID,
    recording_id: UUID,
    body: RecordingCancel,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = cancel_recording(
                db,
                session_id=session_id,
                session_task_id=task_id,
                recording_id=recording_id,
                attempt_id=body.attempt_id,
            )
            return view(row, session_id, task_id)
        except ValueError as exc:
            conflict(exc)


def _preflight_upload(db, session_id, task_id, attempt_id, recording_id):
    with db.begin():
        task, _, revision = task_row(db, session_id, task_id)
        attempt = db.get(Attempt, attempt_id)
        row = db.get(Recording, recording_id)
        owner = db.get(AssessmentSession, session_id)
        if (
            owner.status != "IN_PROGRESS"
            or revision.task_type != "repetition"
            or task.current_attempt_id != attempt_id
            or attempt is None
            or attempt.status != "OPEN"
            or row is None
            or row.attempt_id != attempt_id
            or row.status not in ("STARTED", "SAVED")
        ):
            raise HTTPException(409, "Recording is not available for upload")


def _persist_upload(
    db, session_id, task_id, attempt_id, recording_id, credential_id, ref, sha, media_type, store
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, credential_id)
        task_row(db, session_id, task_id)
        try:
            row = save_recording(
                db,
                session_id=session_id,
                session_task_id=task_id,
                attempt_id=attempt_id,
                recording_id=recording_id,
                storage_ref=ref,
                sha256=sha,
                media_type=media_type,
                store=store,
            )
            return view(row, session_id, task_id)
        except ValueError as exc:
            conflict(exc)


@router.post("/recordings/{recording_id}/content")
async def upload(
    session_id: UUID,
    task_id: UUID,
    recording_id: UUID,
    request: Request,
    attempt_id: UUID = attempt_header,
    _auth: UUID = write_auth,
    db: Session = write_db,
    store: RecordingStore = recording_store,
):
    # Authentication and a short preflight precede body consumption; the commit rechecks both.
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"audio/[a-z0-9][a-z0-9!#$&^_.+\-]*", media_type):
        raise HTTPException(422, "An audio media type is required")
    await run_in_threadpool(_preflight_upload, db, session_id, task_id, attempt_id, recording_id)
    digest = hashlib.sha256()
    file, path = await run_in_threadpool(store.staging_file)
    size = 0
    try:
        with file:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(file.write, chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if not size:
                raise HTTPException(422, "Recording is empty")
            await run_in_threadpool(file.flush)
            await run_in_threadpool(os.fsync, file.fileno())
        sha = digest.hexdigest()
        try:
            ref = await run_in_threadpool(store.commit, path, sha)
        except ValueError as exc:
            raise HTTPException(409, "Recording storage integrity failure") from exc
        return await run_in_threadpool(
            _persist_upload,
            db,
            session_id,
            task_id,
            attempt_id,
            recording_id,
            _auth,
            ref,
            sha,
            media_type,
            store,
        )
    finally:
        path.unlink(missing_ok=True)


@router.get("/recordings")
def list_recordings(
    session_id: UUID,
    task_id: UUID,
    response: Response,
    _auth: UUID = read_auth,
    db: Session = read_db,
):
    task, _, revision = task_row(db, session_id, task_id)
    if revision.task_type != "repetition":
        raise HTTPException(404, "Recordings not available")
    rows = (
        []
        if task.current_attempt_id is None
        else db.scalars(
            select(Recording)
            .where(Recording.attempt_id == task.current_attempt_id)
            .order_by(Recording.recording_id)
        ).all()
    )
    response.headers["Cache-Control"] = "no-store"
    return [view(row, session_id, task_id) for row in rows]


@router.get("/recordings/{recording_id}/content")
def recording_content(
    session_id: UUID,
    task_id: UUID,
    recording_id: UUID,
    _auth: UUID = read_auth,
    db: Session = read_db,
    store: RecordingStore = recording_store,
):
    task, _, revision = task_row(db, session_id, task_id)
    row = db.scalar(
        select(Recording)
        .join(Attempt, Attempt.attempt_id == Recording.attempt_id)
        .where(
            Recording.recording_id == recording_id,
            Attempt.session_task_id == task.session_task_id,
            Recording.status == "SAVED",
        )
    )
    if revision.task_type != "repetition" or row is None:
        raise HTTPException(404, "Recording not found")
    try:
        data = store.read(row.storage_ref, row.sha256)
    except ValueError as exc:
        raise HTTPException(404, "Recording unavailable") from exc
    return Response(data, media_type=row.media_type, headers={"Cache-Control": "no-store"})


@router.post("/submissions/repetition")
def submit(
    session_id: UUID,
    task_id: UUID,
    body: RepetitionSubmit,
    _auth: UUID = write_auth,
    db: Session = write_db,
    store: RecordingStore = recording_store,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = submit_repetition(
                db, session_id=session_id, session_task_id=task_id, store=store, **body.model_dump()
            )
            return {
                "submission_id": row.submission_id,
                "attempt_id": row.attempt_id,
                "status": "ACCEPTED",
            }
        except ValueError as exc:
            conflict(exc)
