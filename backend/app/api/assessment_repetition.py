"""Session-authorized simple recording and submission endpoints."""

import hashlib
import os
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
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
from app.api.teacher_api import get_audio_store, get_db
from app.media.audio_inspection import AudioError, AudioTools, inspect_audio
from app.media.fixed_audio import AudioStore
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.sessions.assessment_session import AssessmentSession, Attempt
from app.sessions.submission import Playback
from app.task_types import speaking
from app.task_types.qa import submit_qa
from app.task_types.repetition import submit_repetition
from app.task_types.simple_recording import cancel_recording, save_recording, start_recording
from app.task_types.speaking import SpeakingSegment
from app.task_types.speaking_asr import SpeakingASR
from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest, request_help

router = APIRouter(prefix="/assessment/sessions/{session_id}/tasks/{task_id}")
read_auth = Depends(require_read_session_access)
write_auth = Depends(require_session_access)
read_db = Depends(assessment_read_db)
write_db = Depends(get_db, use_cache=False)
attempt_header = Header(alias="X-Attempt-Id")


def get_recording_store():
    return RecordingStore()


recording_store = Depends(get_recording_store)
audio_store = Depends(get_audio_store)


class RecordingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    request_id: str = Field(min_length=1, pattern=r"\S")


class RecordingCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


class RecordingSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    recording_id: UUID
    submit_request_id: str = Field(min_length=1, pattern=r"\S")


class ASRRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    request_id: str = Field(min_length=1, pattern=r"\S")
    expected_attempts: int = Field(ge=0)
    accept_unknown: StrictBool = False


class HelpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    request_id: str = Field(min_length=1, pattern=r"\S")


class HelpRetry(HelpInput):
    expected_attempts: int = Field(ge=0)
    accept_unknown: StrictBool = False


class HelpVoid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    accept_unknown: StrictBool = False


@router.post("/help-requests")
def create_help(
    session_id: UUID,
    task_id: UUID,
    body: HelpInput,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = request_help(
                db, session_id=session_id, session_task_id=task_id, **body.model_dump()
            )
            return {"help_request_id": row.help_request_id, "status": row.status}
        except ValueError as exc:
            conflict(exc)


@router.post("/help-requests/{help_id}/retry")
def retry_help(
    session_id: UUID,
    task_id: UUID,
    help_id: UUID,
    body: HelpRetry,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            speaking.current(db, session_id, task_id, body.attempt_id)
            row = db.scalar(
                select(SpeakingHelpRequest)
                .where(
                    SpeakingHelpRequest.help_request_id == help_id,
                    SpeakingHelpRequest.attempt_id == body.attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Help request not found")
            if row.retry_request_id == body.request_id:
                if row.retry_expected_attempts != body.expected_attempts:
                    raise ValueError("Retry request conflicts")
                return {"help_request_id": row.help_request_id, "status": row.status}
            if row.attempts != body.expected_attempts or row.status not in ("FAILED", "UNKNOWN"):
                raise ValueError("Help request cannot be retried")
            if row.status == "UNKNOWN" and not body.accept_unknown:
                raise ValueError("Unknown external outcome requires explicit acknowledgment")
            row.status, row.error_code = "WAITING", None
            row.retry_request_id, row.retry_expected_attempts = (
                body.request_id,
                body.expected_attempts,
            )
            return {"help_request_id": row.help_request_id, "status": row.status}
        except ValueError as exc:
            conflict(exc)


@router.post("/help-requests/{help_id}/void")
def void_help(
    session_id: UUID,
    task_id: UUID,
    help_id: UUID,
    body: HelpVoid,
    _auth: UUID = write_auth,
    db: Session = write_db,
    store: AudioStore = audio_store,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            speaking.current(db, session_id, task_id, body.attempt_id)
            row = db.scalar(
                select(SpeakingHelpRequest)
                .where(
                    SpeakingHelpRequest.help_request_id == help_id,
                    SpeakingHelpRequest.attempt_id == body.attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Help request not found")
            if row.status == "VOIDED":
                return {"help_request_id": row.help_request_id, "status": row.status}
            if row.status == "READY":
                audio = db.scalar(
                    select(GuidanceAudio)
                    .where(GuidanceAudio.help_request_id == help_id)
                    .with_for_update()
                )
                playback = (
                    db.scalar(
                        select(Playback)
                        .where(Playback.guidance_audio_id == audio.audio_id)
                        .order_by(Playback.playback_id)
                        .limit(1)
                    )
                    if audio
                    else None
                )
                if audio is None or audio.attempt_id != body.attempt_id:
                    raise ValueError("Guidance audio not available")
                if (
                    db.scalar(
                        select(Playback.playback_id).where(
                            Playback.attempt_id == body.attempt_id, Playback.status == "STARTED"
                        )
                    )
                    is not None
                ):
                    raise ValueError("Playback in progress")
                if audio.status == "UNKNOWN" and not body.accept_unknown:
                    raise ValueError("Unknown external outcome requires explicit acknowledgment")
                resource_unavailable = False
                if audio.status in ("READY", "SAVED"):
                    try:
                        store.read(audio.storage_ref, audio.content_sha256)
                    except (ValueError, OSError):
                        resource_unavailable = True
                if (
                    audio.status not in ("FAILED", "UNKNOWN")
                    and not resource_unavailable
                    and not (
                        audio.status == "READY"
                        and playback is not None
                        and playback.status == "FAILED"
                    )
                ):
                    raise ValueError("Guidance delivery cannot be voided")
                row.error_code = (
                    "GUIDANCE_AUDIO_UNAVAILABLE" if resource_unavailable else "DELIVERY_FAILED"
                )
                row.execution_history = [
                    *row.execution_history,
                    {"status": row.error_code, "audio_id": str(audio.audio_id)},
                ]
            elif row.status not in ("FAILED", "UNKNOWN"):
                raise ValueError("Help request cannot be voided")
            if row.status == "UNKNOWN" and not body.accept_unknown:
                raise ValueError("Unknown external outcome requires explicit acknowledgment")
            row.status = "VOIDED"
            return {"help_request_id": row.help_request_id, "status": row.status}
        except ValueError as exc:
            conflict(exc)


@router.post("/help-requests/{help_id}/audio/retry")
def retry_guidance_audio(
    session_id: UUID,
    task_id: UUID,
    help_id: UUID,
    body: HelpRetry,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        try:
            speaking.current(db, session_id, task_id, body.attempt_id)
            help_row = db.scalar(
                select(SpeakingHelpRequest)
                .where(
                    SpeakingHelpRequest.help_request_id == help_id,
                    SpeakingHelpRequest.attempt_id == body.attempt_id,
                )
                .with_for_update()
            )
            audio = db.scalar(
                select(GuidanceAudio)
                .where(GuidanceAudio.help_request_id == help_id)
                .with_for_update()
            )
            if help_row is None or help_row.status != "READY" or audio is None:
                raise ValueError("Guidance audio not available")
            if audio.retry_request_id == body.request_id:
                if audio.retry_expected_attempts != body.expected_attempts:
                    raise ValueError("Retry request conflicts")
                return {"audio_id": audio.audio_id, "status": audio.status}
            if (
                audio.status not in ("FAILED", "UNKNOWN")
                or audio.attempts != body.expected_attempts
            ):
                raise ValueError("Guidance audio cannot be retried")
            if audio.status == "UNKNOWN" and not body.accept_unknown:
                raise ValueError("Unknown external outcome requires explicit acknowledgment")
            audio.status, audio.error_code = "WAITING", None
            audio.retry_request_id, audio.retry_expected_attempts = (
                body.request_id,
                body.expected_attempts,
            )
            return {"audio_id": audio.audio_id, "status": audio.status}
        except ValueError as exc:
            conflict(exc)


@router.post("/segments/{segment_id}/asr/retry")
def retry_asr(
    session_id: UUID,
    task_id: UUID,
    segment_id: UUID,
    body: ASRRetry,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            speaking.current(db, session_id, task_id, body.attempt_id)
            job = db.scalar(
                select(SpeakingASR)
                .join(SpeakingSegment)
                .where(
                    SpeakingSegment.segment_id == segment_id,
                    SpeakingSegment.attempt_id == body.attempt_id,
                )
                .with_for_update()
            )
            if job is None:
                raise ValueError("Recognition operation not found")
            if job.retry_request_id == body.request_id:
                if job.retry_expected_attempts != body.expected_attempts:
                    raise ValueError("Retry request conflicts")
                return {"operation_id": job.operation_id, "status": job.status}
            if job.attempts != body.expected_attempts or job.status not in ("FAILED", "UNKNOWN"):
                raise ValueError("Recognition is not available for retry")
            if job.status == "UNKNOWN" and not body.accept_unknown:
                raise ValueError("Unknown external outcome requires explicit acknowledgment")
            job.status, job.error_code = "WAITING", None
            job.retry_request_id, job.retry_expected_attempts = (
                body.request_id,
                body.expected_attempts,
            )
            return {"operation_id": job.operation_id, "status": job.status}
        except ValueError as exc:
            conflict(exc)


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
        _, _, revision = task_row(db, session_id, task_id)
        try:
            action = speaking.start if revision.task_type == "speaking" else start_recording
            row = action(db, session_id=session_id, session_task_id=task_id, **body.model_dump())
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
        _, _, revision = task_row(db, session_id, task_id)
        try:
            action = speaking.cancel if revision.task_type == "speaking" else cancel_recording
            row = action(
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
            or revision.task_type not in ("repetition", "qa", "speaking")
            or task.current_attempt_id != attempt_id
            or attempt is None
            or attempt.status != "OPEN"
            or row is None
            or row.attempt_id != attempt_id
            or row.status not in ("STARTED", "SAVED")
        ):
            raise HTTPException(409, "Recording is not available for upload")
        return revision.task_type, row.sha256


def _persist_upload(
    db,
    session_id,
    task_id,
    attempt_id,
    recording_id,
    credential_id,
    ref,
    sha,
    media_type,
    store,
    audio_info,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, credential_id)
        _, _, revision = task_row(db, session_id, task_id)
        try:
            action = speaking.save if revision.task_type == "speaking" else save_recording
            extra = (
                {"duration_seconds": audio_info.duration_seconds}
                if revision.task_type == "speaking"
                else {}
            )
            row = action(
                db,
                session_id=session_id,
                session_task_id=task_id,
                attempt_id=attempt_id,
                recording_id=recording_id,
                storage_ref=ref,
                sha256=sha,
                media_type=media_type,
                store=store,
                **extra,
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
    task_type, existing_digest = await run_in_threadpool(
        _preflight_upload, db, session_id, task_id, attempt_id, recording_id
    )
    digest = hashlib.sha256()
    file, path = await run_in_threadpool(store.staging_file)
    size = 0
    audio_tools = AudioTools() if task_type == "speaking" else None
    try:
        with file:
            async for chunk in request.stream():
                if chunk:
                    size += len(chunk)
                    if audio_tools is not None and size > audio_tools.max_input_bytes:
                        raise HTTPException(413, detail={"code": "AUDIO_RESOURCE_LIMIT"})
                    await run_in_threadpool(file.write, chunk)
                    digest.update(chunk)
            if not size:
                if audio_tools is not None:
                    raise HTTPException(422, detail={"code": "AUDIO_UNRECOGNIZED"})
                raise HTTPException(422, "Recording is empty")
            await run_in_threadpool(file.flush)
            await run_in_threadpool(os.fsync, file.fileno())
        sha = digest.hexdigest()
        if task_type == "speaking" and existing_digest is not None and sha != existing_digest:
            raise HTTPException(409, "Recording upload conflicts")
        audio_info = None
        if task_type == "speaking":
            try:
                audio_info = await run_in_threadpool(inspect_audio, path, audio_tools)
            except AudioError as exc:
                if exc.code in {
                    "AUDIO_UNRECOGNIZED",
                    "AUDIO_NO_STREAM",
                    "AUDIO_METADATA_INCOMPLETE",
                    "AUDIO_DECODE_FAILED",
                    "AUDIO_STREAM_CORRUPT",
                }:
                    status, code = 422, exc.code
                elif exc.code == "AUDIO_RESOURCE_LIMIT":
                    status, code = 413, exc.code
                elif exc.code == "AUDIO_TOOL_TIMEOUT":
                    status, code = 504, exc.code
                elif exc.code == "AUDIO_TOOL_UNAVAILABLE":
                    status, code = 503, exc.code
                else:
                    status = 500
                    code = (
                        "AUDIO_TOOL_FAILED"
                        if exc.code == "AUDIO_TOOL_FAILED"
                        else "AUDIO_INSPECTION_FAILED"
                    )
                raise HTTPException(status, detail={"code": code}) from exc
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
            audio_info,
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
    if revision.task_type not in ("repetition", "qa", "speaking"):
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
    if revision.task_type not in ("repetition", "qa", "speaking") or row is None:
        raise HTTPException(404, "Recording not found")
    try:
        data = store.read(row.storage_ref, row.sha256)
    except ValueError as exc:
        raise HTTPException(404, "Recording unavailable") from exc
    return Response(data, media_type=row.media_type, headers={"Cache-Control": "no-store"})


def _submit_recording(db, session_id, task_id, body, credential_id, store, handler):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, credential_id)
        task_row(db, session_id, task_id)
        try:
            row = handler(
                db, session_id=session_id, session_task_id=task_id, store=store, **body.model_dump()
            )
            return {
                "submission_id": row.submission_id,
                "attempt_id": row.attempt_id,
                "status": "ACCEPTED",
            }
        except ValueError as exc:
            conflict(exc)


@router.post("/submissions/repetition")
def submit(
    session_id: UUID,
    task_id: UUID,
    body: RecordingSubmit,
    _auth: UUID = write_auth,
    db: Session = write_db,
    store: RecordingStore = recording_store,
):
    return _submit_recording(db, session_id, task_id, body, _auth, store, submit_repetition)


@router.post("/submissions/qa")
def submit_qa_answer(
    session_id: UUID,
    task_id: UUID,
    body: RecordingSubmit,
    _auth: UUID = write_auth,
    db: Session = write_db,
    store: RecordingStore = recording_store,
):
    return _submit_recording(db, session_id, task_id, body, _auth, store, submit_qa)
