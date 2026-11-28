"""Shared operations for simple single-recording task types."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.media.recording import Recording, recording_busy
from app.media.recording_store import RecordingStore
from app.sessions.assessment_session import AssessmentSession, _identity
from app.sessions.submission import (
    Submission,
    _accept_submission,
    _busy,
    _completed,
    _locked,
    _open,
    _revision,
    effective_replay_ids,
)


def _current(db, session_id, task_id, attempt_id):
    task, attempt = _locked(db, session_id, task_id, attempt_id)
    _open(task, attempt)
    revision = _revision(db, task)
    if revision.task_type not in ("repetition", "qa"):
        raise ValueError("Unsupported task type")
    return task, attempt, revision


def start_recording(
    db: Session, *, session_id: UUID, session_task_id: UUID, attempt_id: UUID, request_id: str
):
    _identity(request_id)
    _, _, revision = _current(db, session_id, session_task_id, attempt_id)
    previous = db.scalar(
        select(Recording).where(
            Recording.attempt_id == attempt_id, Recording.request_id == request_id
        )
    )
    if previous is not None:
        return previous
    if (
        (revision.guide_text and not _completed(db, attempt_id, "GUIDE"))
        or not _completed(db, attempt_id, "INITIAL_PROMPT")
        or _busy(db, attempt_id)
        or recording_busy(db, attempt_id)
    ):
        raise ValueError("Recording is not allowed in this stage")
    row = Recording(
        recording_id=uuid4(), attempt_id=attempt_id, request_id=request_id, status="STARTED"
    )
    db.add(row)
    db.flush()
    return row


def _recording(db, attempt_id, recording_id):
    row = db.scalar(
        select(Recording)
        .where(Recording.recording_id == recording_id, Recording.attempt_id == attempt_id)
        .with_for_update()
    )
    if row is None:
        raise ValueError("Recording does not belong to attempt")
    return row


def cancel_recording(
    db: Session, *, session_id: UUID, session_task_id: UUID, attempt_id: UUID, recording_id: UUID
):
    _current(db, session_id, session_task_id, attempt_id)
    row = _recording(db, attempt_id, recording_id)
    if row.status == "SAVED":
        raise ValueError("Saved recording cannot be cancelled")
    if row.status == "STARTED":
        row.status = "CANCELLED"
        db.flush()
    return row


def save_recording(
    db: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    attempt_id: UUID,
    recording_id: UUID,
    storage_ref: str,
    sha256: str,
    media_type: str,
    store: RecordingStore,
):
    _current(db, session_id, session_task_id, attempt_id)
    row = _recording(db, attempt_id, recording_id)
    if row.status == "SAVED":
        if (row.storage_ref, row.sha256, row.media_type) != (storage_ref, sha256, media_type):
            raise ValueError("Recording upload conflicts")
        store.verify(row.storage_ref, row.sha256)
        return row
    if row.status != "STARTED" or _busy(db, attempt_id):
        raise ValueError("Recording is not active")
    store.verify(storage_ref, sha256)
    row.status, row.storage_ref, row.sha256, row.media_type = (
        "SAVED",
        storage_ref,
        sha256,
        media_type,
    )
    db.flush()
    return row


def submit_recording(
    db: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    attempt_id: UUID,
    submit_request_id: str,
    recording_id: UUID,
    store: RecordingStore,
    task_type: str,
):
    _identity(submit_request_id)
    task, attempt = _locked(db, session_id, session_task_id, attempt_id, allow_closed=True)
    revision = _revision(db, task)
    if revision.task_type != task_type:
        raise ValueError("Unsupported task type")
    existing = db.scalar(
        select(Submission).where(
            Submission.session_task_id == session_task_id,
            Submission.submit_request_id == submit_request_id,
        )
    )
    if existing is not None:
        if existing.attempt_id != attempt_id or existing.inputs.get("recording_id") != str(
            recording_id
        ):
            raise ValueError("Submission request conflicts")
        return existing
    if db.get(AssessmentSession, session_id).status != "IN_PROGRESS":
        raise ValueError("Session is not in progress")
    _open(task, attempt)
    if (
        (revision.guide_text and not _completed(db, attempt_id, "GUIDE"))
        or not _completed(db, attempt_id, "INITIAL_PROMPT")
        or _busy(db, attempt_id)
        or recording_busy(db, attempt_id)
    ):
        raise ValueError("Input is not ready")
    recording = _recording(db, attempt_id, recording_id)
    if recording.status != "SAVED":
        raise ValueError("Recording is not saved")
    store.verify(recording.storage_ref, recording.sha256)
    replay_ids = effective_replay_ids(db, attempt_id)
    return _accept_submission(
        db,
        task=task,
        attempt=attempt,
        request_id=submit_request_id,
        inputs={
            "recording_id": str(recording_id),
            "replay_playback_ids": [str(i) for i in replay_ids],
        },
    )
