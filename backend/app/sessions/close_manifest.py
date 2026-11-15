"""Atomic closure of an authorized session; callers own authentication and transaction."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.papers.paper import PaperEntry
from app.sessions.assessment_session import (
    AssessmentSession,
    Attempt,
    SessionTask,
    _identity,
    _transaction,
)
from app.sessions.submission import Submission


class CloseManifest(Base):
    __tablename__ = "close_manifests"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_manifest_session"),
        UniqueConstraint("manifest_id", "session_id", name="uq_manifest_session_pair"),
    )
    manifest_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessment_sessions.session_id"), nullable=False
    )
    close_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CloseManifestEntry(Base):
    __tablename__ = "close_manifest_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["manifest_id", "session_id"],
            ["close_manifests.manifest_id", "close_manifests.session_id"],
            name="fk_manifest_entry_manifest_session",
        ),
        ForeignKeyConstraint(
            ["session_task_id", "session_id"],
            ["session_tasks.session_task_id", "session_tasks.session_id"],
            name="fk_manifest_entry_task_session",
        ),
        ForeignKeyConstraint(
            ["submission_id", "session_task_id"],
            ["submissions.submission_id", "submissions.session_task_id"],
            name="fk_manifest_entry_submission_task",
        ),
        UniqueConstraint("manifest_id", "submission_id", name="uq_manifest_entry_submission"),
    )
    manifest_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    session_task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class SettlementIntent(Base):
    __tablename__ = "settlement_intents"
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_manifests.manifest_id"), primary_key=True
    )


class IncompleteSession(ValueError):
    def __init__(self, task_ids: list[UUID]):
        super().__init__("Session has incomplete tasks")
        self.task_ids = task_ids


class SessionTaskIntegrityError(ValueError):
    def __init__(self, missing_paper_entry_ids: list[UUID]):
        super().__init__("Session task set does not match the published paper")
        self.missing_paper_entry_ids = missing_paper_entry_ids


def close_session(session: Session, *, session_id: UUID, close_request_id: str) -> CloseManifest:
    """Caller must authorize session access before invoking, including idempotent retries."""
    _identity(close_request_id)
    _transaction(session)
    owner = session.scalar(
        select(AssessmentSession)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner is None:
        raise ValueError("Session does not exist")
    existing = session.scalar(select(CloseManifest).where(CloseManifest.session_id == session_id))
    if existing is not None:
        if existing.close_request_id != close_request_id:
            raise ValueError("Session already closed with another request")
        return existing
    if owner.status != "IN_PROGRESS":
        raise ValueError("Session is not in progress")
    tasks = session.scalars(
        select(SessionTask)
        .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
        .where(SessionTask.session_id == session_id)
        .order_by(PaperEntry.position)
        .with_for_update(of=SessionTask)
        .execution_options(populate_existing=True)
    ).all()
    expected_entries = session.scalars(
        select(PaperEntry.paper_entry_id)
        .where(PaperEntry.paper_id == owner.paper_id)
        .order_by(PaperEntry.position)
    ).all()
    task_entries = {task.paper_entry_id for task in tasks}
    missing_entries = [entry_id for entry_id in expected_entries if entry_id not in task_entries]
    if missing_entries or task_entries != set(expected_entries):
        raise SessionTaskIntegrityError(missing_entries)
    missing, accepted = [], []
    for task in tasks:
        attempt = None
        if task.current_attempt_id is not None:
            attempt = session.scalar(
                select(Attempt)
                .where(
                    Attempt.attempt_id == task.current_attempt_id,
                    Attempt.session_task_id == task.session_task_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        submission = None
        if attempt is not None and attempt.status == "SEALED":
            submission = session.scalar(
                select(Submission).where(
                    Submission.attempt_id == attempt.attempt_id,
                    Submission.session_task_id == task.session_task_id,
                )
            )
        if submission is None:
            missing.append(task.session_task_id)
        else:
            accepted.append((task.session_task_id, submission.submission_id))
    if missing:
        raise IncompleteSession(missing)
    manifest = CloseManifest(
        manifest_id=uuid4(), session_id=session_id, close_request_id=close_request_id
    )
    session.add(manifest)
    session.flush()
    session.add_all(
        [
            CloseManifestEntry(
                manifest_id=manifest.manifest_id,
                session_id=session_id,
                session_task_id=task_id,
                submission_id=submission_id,
            )
            for task_id, submission_id in accepted
        ]
    )
    session.add(SettlementIntent(manifest_id=manifest.manifest_id))
    session.flush()
    owner.status = "INPUT_CLOSED"
    session.flush()
    return manifest


def manifest_entries(session: Session, manifest_id: UUID) -> list[CloseManifestEntry]:
    return list(
        session.scalars(
            select(CloseManifestEntry)
            .join(SessionTask, SessionTask.session_task_id == CloseManifestEntry.session_task_id)
            .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
            .where(CloseManifestEntry.manifest_id == manifest_id)
            .order_by(PaperEntry.position)
        )
    )
