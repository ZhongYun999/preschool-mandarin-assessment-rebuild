"""Playback facts and the first formal submission handler.

Internal services require caller authorization and a caller-owned transaction.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.media.fixed_audio import FixedAudio
from app.media.recording import recording_busy
from app.papers.paper import EvaluationPlan, PaperEntry
from app.sessions.assessment_session import AssessmentSession, Attempt, _identity, _lock_task
from app.taskbank.models import TaskRevision


class Playback(Base):
    __tablename__ = "playbacks"
    __table_args__ = (
        UniqueConstraint("attempt_id", "request_id", name="uq_playback_request"),
        CheckConstraint(
            "kind IN ('GUIDE','INITIAL_PROMPT','USER_REPLAY','TECHNICAL_RETRY')",
            name="ck_playback_kind",
        ),
        CheckConstraint("status IN ('STARTED','COMPLETED','FAILED')", name="ck_playback_status"),
    )
    playback_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    audio_id: Mapped[UUID] = mapped_column(
        ForeignKey("fixed_audio_requests.audio_id"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    retry_of_id: Mapped[UUID | None] = mapped_column(ForeignKey("playbacks.playback_id"))


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_submission_attempt"),
        UniqueConstraint("submission_id", "session_task_id", name="uq_submission_task"),
        UniqueConstraint("session_task_id", "submit_request_id", name="uq_submission_request"),
        ForeignKeyConstraint(
            ["attempt_id", "session_task_id"],
            ["attempts.attempt_id", "attempts.session_task_id"],
            name="fk_submission_attempt_task",
        ),
        CheckConstraint("jsonb_typeof(inputs) = 'object'", name="ck_submission_inputs"),
    )
    submission_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("session_tasks.session_task_id"), nullable=False
    )
    submit_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationIntent(Base):
    __tablename__ = "evaluation_intents"
    __table_args__ = (
        UniqueConstraint("submission_id", "requirement_index", name="uq_intent_requirement"),
        UniqueConstraint(
            "intent_id",
            "submission_id",
            "paper_entry_id",
            "requirement_index",
            name="uq_intent_identity",
        ),
        Index(
            "ix_evaluation_intents_pending",
            "intent_id",
            postgresql_where=text("scheduled_at IS NULL"),
        ),
    )
    intent_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.submission_id"), nullable=False
    )
    paper_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_plans.paper_entry_id"), nullable=False
    )
    requirement_index: Mapped[int] = mapped_column(nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _locked(
    session: Session, session_id: UUID, task_id: UUID, attempt_id: UUID, *, allow_closed=False
):
    task = _lock_task(session, session_id, task_id, allow_closed=allow_closed)
    attempt = session.scalar(
        select(Attempt)
        .where(Attempt.attempt_id == attempt_id, Attempt.session_task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if attempt is None:
        raise ValueError("Attempt does not belong to task")
    return task, attempt


def _revision(session: Session, task):
    return session.scalar(
        select(TaskRevision)
        .join(PaperEntry, PaperEntry.task_revision_id == TaskRevision.task_revision_id)
        .where(PaperEntry.paper_entry_id == task.paper_entry_id)
    )


def _open(task, attempt):
    if task.current_attempt_id != attempt.attempt_id or attempt.status != "OPEN":
        raise ValueError("Attempt is not current and open")


def _completed(session, attempt_id, kind):
    rows = session.scalars(select(Playback).where(Playback.attempt_id == attempt_id)).all()
    originals = {row.playback_id for row in rows if row.kind == kind}
    return any(
        row.status == "COMPLETED"
        and (row.kind == kind or (row.kind == "TECHNICAL_RETRY" and row.retry_of_id in originals))
        for row in rows
    )


def _busy(session, attempt_id):
    return (
        session.scalar(
            select(Playback.playback_id)
            .where(Playback.attempt_id == attempt_id, Playback.status == "STARTED")
            .limit(1)
        )
        is not None
    )


def start_playback(
    session: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    attempt_id: UUID,
    request_id: str,
    kind: str,
    retry_of_id: UUID | None = None,
) -> Playback:
    _identity(request_id)
    task, attempt = _locked(session, session_id, session_task_id, attempt_id)
    _open(task, attempt)
    previous = session.scalar(
        select(Playback).where(Playback.attempt_id == attempt_id, Playback.request_id == request_id)
    )
    if previous:
        if (previous.kind, previous.retry_of_id) != (kind, retry_of_id):
            raise ValueError("Playback request conflicts")
        return previous
    revision = _revision(session, task)
    if revision.task_type not in ("instruction", "repetition", "qa"):
        raise ValueError("Unsupported task type")
    if revision.task_type in ("repetition", "qa") and recording_busy(session, attempt_id):
        raise ValueError("Recording in progress")
    if _busy(session, attempt_id):
        raise ValueError("Playback in progress")
    guide_done = not revision.guide_text or _completed(session, attempt_id, "GUIDE")
    initial_done = _completed(session, attempt_id, "INITIAL_PROMPT")
    if kind == "TECHNICAL_RETRY":
        original = session.get(Playback, retry_of_id) if retry_of_id else None
        if (
            original is None
            or original.attempt_id != attempt_id
            or original.status != "FAILED"
            or original.kind not in ("GUIDE", "INITIAL_PROMPT", "USER_REPLAY")
        ):
            raise ValueError("Invalid technical retry")
        role = "guide" if original.kind == "GUIDE" else "prompt"
        if (
            (original.kind == "GUIDE" and guide_done)
            or (original.kind == "INITIAL_PROMPT" and initial_done)
            or session.scalar(
                select(Playback.playback_id).where(
                    Playback.retry_of_id == original.playback_id, Playback.status == "COMPLETED"
                )
            )
            is not None
        ):
            raise ValueError("Delivery already completed")
    else:
        if retry_of_id is not None:
            raise ValueError("Unexpected retry identity")
        if kind == "GUIDE" and revision.guide_text and not guide_done and not initial_done:
            role = "guide"
        elif (
            kind == "INITIAL_PROMPT"
            and guide_done
            and not initial_done
            or kind == "USER_REPLAY"
            and guide_done
            and initial_done
        ):
            role = "prompt"
        else:
            raise ValueError("Playback is not allowed in this stage")
    audio = session.scalar(
        select(FixedAudio).where(
            FixedAudio.paper_entry_id == task.paper_entry_id, FixedAudio.role == role
        )
    )
    if audio is None or audio.status != "READY":
        raise ValueError("Frozen audio unavailable")
    row = Playback(
        playback_id=uuid4(),
        attempt_id=attempt_id,
        request_id=request_id,
        kind=kind,
        status="STARTED",
        audio_id=audio.audio_id,
        retry_of_id=retry_of_id,
    )
    session.add(row)
    session.flush()
    return row


def finish_playback(
    session: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    attempt_id: UUID,
    playback_id: UUID,
    completed: bool,
) -> Playback:
    task, attempt = _locked(session, session_id, session_task_id, attempt_id)
    _open(task, attempt)
    row = session.scalar(
        select(Playback)
        .where(Playback.playback_id == playback_id, Playback.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ValueError("Playback does not belong to attempt")
    result = "COMPLETED" if completed else "FAILED"
    if row.status != "STARTED":
        if row.status != result:
            raise ValueError("Playback result conflicts")
        return row
    row.status = result
    session.flush()
    return row


def submit_instruction(
    session: Session,
    *,
    session_id: UUID,
    session_task_id: UUID,
    attempt_id: UUID,
    submit_request_id: str,
    selected_choice_id: str,
) -> Submission:
    _identity(submit_request_id)
    task, attempt = _locked(session, session_id, session_task_id, attempt_id, allow_closed=True)
    existing = session.scalar(
        select(Submission).where(
            Submission.session_task_id == session_task_id,
            Submission.submit_request_id == submit_request_id,
        )
    )
    if existing:
        if (
            existing.attempt_id != attempt_id
            or existing.inputs.get("selected_choice_id") != selected_choice_id
        ):
            raise ValueError("Submission request conflicts")
        return existing
    if session.get(AssessmentSession, session_id).status != "IN_PROGRESS":
        raise ValueError("Session is not in progress")
    revision = _revision(session, task)
    if revision.task_type != "instruction" or selected_choice_id not in ("A", "B", "C"):
        raise ValueError("Invalid instruction answer")
    if (
        (revision.guide_text and not _completed(session, attempt_id, "GUIDE"))
        or not _completed(session, attempt_id, "INITIAL_PROMPT")
        or _busy(session, attempt_id)
    ):
        raise ValueError("Input is not ready")
    replay_ids = list(
        session.scalars(
            select(Playback.playback_id)
            .where(
                Playback.attempt_id == attempt_id,
                Playback.kind == "USER_REPLAY",
                Playback.status == "COMPLETED",
            )
            .order_by(Playback.playback_id)
        )
    )
    return _accept_submission(
        session,
        task=task,
        attempt=attempt,
        request_id=submit_request_id,
        inputs={
            "selected_choice_id": selected_choice_id,
            "replay_playback_ids": [str(p) for p in replay_ids],
        },
    )


def _accept_submission(session: Session, *, task, attempt: Attempt, request_id: str, inputs: dict):
    """Persist validated inputs, processing intent and sealing in the caller's locked transaction."""
    _open(task, attempt)
    plan = session.get(EvaluationPlan, task.paper_entry_id)
    if plan is None:
        raise ValueError("Frozen evaluation plan missing")
    submission = Submission(
        submission_id=uuid4(),
        attempt_id=attempt.attempt_id,
        session_task_id=task.session_task_id,
        submit_request_id=request_id,
        inputs=inputs,
    )
    session.add(submission)
    session.flush()
    session.add_all(
        [
            EvaluationIntent(
                intent_id=uuid4(),
                submission_id=submission.submission_id,
                paper_entry_id=task.paper_entry_id,
                requirement_index=i,
            )
            for i, _ in enumerate(plan.requirements)
        ]
    )
    attempt.status = "SEALED"
    session.flush()
    return submission
