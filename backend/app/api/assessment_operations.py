"""Authorized assessment input operations and frozen resource access."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.assessment_auth import (
    assessment_read_db,
    confirm_write_access,
    require_read_session_access,
    require_session_access,
)
from app.api.teacher_api import get_audio_store, get_db, get_image_store
from app.media.fixed_audio import AudioStore, FixedAudio
from app.papers.paper import PaperEntry
from app.sessions.assessment_session import Attempt, SessionTask, redo_attempt, start_attempt
from app.sessions.close_manifest import IncompleteSession, SessionTaskIntegrityError, close_session
from app.sessions.submission import Playback, finish_playback, start_playback, submit_instruction
from app.taskbank.image_store import ImageStore
from app.taskbank.models import Asset, TaskRevision

router = APIRouter(prefix="/assessment/sessions/{session_id}")
read_auth = Depends(require_read_session_access)
write_auth = Depends(require_session_access)
read_db = Depends(assessment_read_db)
write_db = Depends(get_db, use_cache=False)
audio_store = Depends(get_audio_store)
image_store = Depends(get_image_store)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaybackInput(Input):
    attempt_id: UUID
    request_id: str = Field(min_length=1, pattern=r"\S")
    kind: Literal["GUIDE", "INITIAL_PROMPT", "USER_REPLAY", "TECHNICAL_RETRY"]
    retry_of_id: UUID | None = None


class FinishInput(Input):
    attempt_id: UUID
    completed: StrictBool


class SubmitInput(Input):
    attempt_id: UUID
    submit_request_id: str = Field(min_length=1, pattern=r"\S")
    selected_choice_id: Literal["A", "B", "C"]


class RedoInput(Input):
    redo_request_id: str = Field(min_length=1, pattern=r"\S")
    expected_attempt_id: UUID | None


class CloseInput(Input):
    close_request_id: str = Field(min_length=1, pattern=r"\S")


def task_row(db, session_id, task_id):
    row = db.execute(
        select(SessionTask, PaperEntry, TaskRevision)
        .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
        .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
        .where(SessionTask.session_id == session_id, SessionTask.session_task_id == task_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Session task not found")
    return row


def attempt_view(attempt):
    return {"attempt_id": attempt.attempt_id, "status": attempt.status}


def playback_view(row, session_id, task_id):
    return {
        "playback_id": row.playback_id,
        "attempt_id": row.attempt_id,
        "request_id": row.request_id,
        "kind": row.kind,
        "status": row.status,
        "retry_of_id": row.retry_of_id,
        "audio_id": row.audio_id,
        "audio_url": f"/assessment/sessions/{session_id}/tasks/{task_id}/audio/{row.audio_id}",
    }


def conflict(exc):
    raise HTTPException(409, str(exc)) from exc


@router.get("/tasks/{task_id}/content")
def content(
    session_id: UUID,
    task_id: UUID,
    response: Response,
    _auth: UUID = read_auth,
    db: Session = read_db,
):
    task, entry, revision = task_row(db, session_id, task_id)
    if revision.task_type not in ("instruction", "repetition", "qa", "speaking"):
        raise HTTPException(404, "Content not available")
    base = f"/assessment/sessions/{session_id}/tasks/{task_id}"
    audio = {
        a.role: f"{base}/audio/{a.audio_id}"
        for a in db.scalars(
            select(FixedAudio).where(
                FixedAudio.paper_entry_id == entry.paper_entry_id,
                FixedAudio.task_revision_id == revision.task_revision_id,
                FixedAudio.status == "READY",
            )
        )
    }
    response.headers["Cache-Control"] = "no-store"
    return {
        "task_id": revision.task_id,
        "task_revision_id": revision.task_revision_id,
        "paper_entry_id": entry.paper_entry_id,
        "session_task_id": task.session_task_id,
        "position": entry.position,
        "task_type": revision.task_type,
        "prompt_text": revision.prompt_text,
        "question_image_url": f"{base}/images/question"
        if revision.question_image_asset_id
        else None,
        "choices": [
            {
                "choice_id": c,
                "image_url": f"{base}/images/{c}"
                if getattr(revision, f"option_{c.lower()}_asset_id")
                else None,
            }
            for c in "ABC"
        ]
        if revision.task_type == "instruction"
        else [],
        "has_guide": bool(revision.guide_text),
        "audio": audio,
    }


@router.get("/tasks/{task_id}/audio/{audio_id}")
def audio(
    session_id: UUID,
    task_id: UUID,
    audio_id: UUID,
    _auth: UUID = read_auth,
    db: Session = read_db,
    store: AudioStore = audio_store,
):
    _, entry, revision = task_row(db, session_id, task_id)
    row = db.scalar(
        select(FixedAudio).where(
            FixedAudio.audio_id == audio_id,
            FixedAudio.paper_entry_id == entry.paper_entry_id,
            FixedAudio.task_revision_id == revision.task_revision_id,
            FixedAudio.status == "READY",
        )
    )
    if row is None:
        raise HTTPException(404, "Audio not found")
    try:
        data = store.read(row.storage_ref, row.content_sha256)
    except ValueError as exc:
        raise HTTPException(404, "Audio unavailable") from exc
    return Response(data, media_type=row.media_type, headers={"Cache-Control": "no-store"})


@router.get("/tasks/{task_id}/images/{slot}")
def image(
    session_id: UUID,
    task_id: UUID,
    slot: Literal["question", "A", "B", "C"],
    _auth: UUID = read_auth,
    db: Session = read_db,
    store: ImageStore = image_store,
):
    _, _, revision = task_row(db, session_id, task_id)
    field = "question_image_asset_id" if slot == "question" else f"option_{slot.lower()}_asset_id"
    asset_id = getattr(revision, field)
    asset = db.get(Asset, asset_id) if asset_id else None
    if asset is None:
        raise HTTPException(404, "Image not found")
    try:
        data = AudioStore(store.root).read(asset.storage_ref, asset.sha256)
    except ValueError as exc:
        raise HTTPException(404, "Image unavailable") from exc
    return Response(data, media_type=asset.media_type, headers={"Cache-Control": "no-store"})


@router.get("/tasks/{task_id}/attempt")
def current_attempt(
    session_id: UUID,
    task_id: UUID,
    response: Response,
    _auth: UUID = read_auth,
    db: Session = read_db,
):
    task, _, _ = task_row(db, session_id, task_id)
    attempt = db.get(Attempt, task.current_attempt_id) if task.current_attempt_id else None
    response.headers["Cache-Control"] = "no-store"
    if attempt is None:
        return {"attempt": None, "playbacks": []}
    rows = db.scalars(select(Playback).where(Playback.attempt_id == attempt.attempt_id)).all()
    return {
        "attempt": attempt_view(attempt),
        "playbacks": [playback_view(p, session_id, task_id) for p in rows],
    }


@router.post("/tasks/{task_id}/attempt")
def begin(session_id: UUID, task_id: UUID, _auth: UUID = write_auth, db: Session = write_db):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            return attempt_view(start_attempt(db, session_id=session_id, session_task_id=task_id))
        except ValueError as exc:
            conflict(exc)


@router.post("/tasks/{task_id}/playbacks")
def playback_start(
    session_id: UUID,
    task_id: UUID,
    body: PlaybackInput,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = start_playback(
                db, session_id=session_id, session_task_id=task_id, **body.model_dump()
            )
            return playback_view(row, session_id, task_id)
        except ValueError as exc:
            conflict(exc)


@router.post("/tasks/{task_id}/playbacks/{playback_id}/finish")
def playback_finish(
    session_id: UUID,
    task_id: UUID,
    playback_id: UUID,
    body: FinishInput,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = finish_playback(
                db,
                session_id=session_id,
                session_task_id=task_id,
                playback_id=playback_id,
                **body.model_dump(),
            )
            return playback_view(row, session_id, task_id)
        except ValueError as exc:
            conflict(exc)


@router.post("/tasks/{task_id}/submissions/instruction")
def submit(
    session_id: UUID,
    task_id: UUID,
    body: SubmitInput,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            row = submit_instruction(
                db, session_id=session_id, session_task_id=task_id, **body.model_dump()
            )
            return {
                "submission_id": row.submission_id,
                "attempt_id": row.attempt_id,
                "status": "ACCEPTED",
            }
        except ValueError as exc:
            conflict(exc)


@router.post("/tasks/{task_id}/redo")
def redo(
    session_id: UUID,
    task_id: UUID,
    body: RedoInput,
    _auth: UUID = write_auth,
    db: Session = write_db,
):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        task_row(db, session_id, task_id)
        try:
            attempt = redo_attempt(
                db, session_id=session_id, session_task_id=task_id, **body.model_dump()
            )
            return {
                **attempt_view(attempt),
                "current_attempt_id": db.get(SessionTask, task_id).current_attempt_id,
            }
        except ValueError as exc:
            conflict(exc)


@router.post("/close")
def close(session_id: UUID, body: CloseInput, _auth: UUID = write_auth, db: Session = write_db):
    with db.begin():
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        confirm_write_access(db, session_id, _auth)
        try:
            row = close_session(db, session_id=session_id, **body.model_dump())
            return {"manifest_id": row.manifest_id, "status": "INPUT_CLOSED"}
        except IncompleteSession as exc:
            raise HTTPException(
                409,
                {"message": str(exc), "missing_session_task_ids": [str(x) for x in exc.task_ids]},
            ) from exc
        except SessionTaskIntegrityError as exc:
            raise HTTPException(
                409,
                {
                    "message": str(exc),
                    "missing_paper_entry_ids": [str(x) for x in exc.missing_paper_entry_ids],
                },
            ) from exc
        except ValueError as exc:
            conflict(exc)
