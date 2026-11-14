"""Session identities and candidate attempts. Callers own the transaction and authorization.

The scope is an opaque, trusted caller identity, not a public access credential. The
future API must authenticate and authorize before invoking any of these functions.
All mutating operations require READ COMMITTED and lock Session before SessionTask.
"""

import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from app.fixed_audio import AudioStore, readiness
from app.paper import EvaluationPlan, Paper, PaperEntry
from app.scoring_registry import PRODUCTION_REGISTRY, Registry
from app.taskbank import TaskRevision


class AssessmentSession(Base):
    __tablename__ = "assessment_sessions"
    __table_args__ = (
        UniqueConstraint("scope", "create_request_id", name="uq_session_create_request"),
        UniqueConstraint("session_id", "paper_id", name="uq_session_paper"),
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'INPUT_CLOSED', 'COMPLETED')", name="ck_session_status"
        ),
        CheckConstraint(
            "jsonb_typeof(review_policy) = 'object' AND review_policy <> '{}'::jsonb",
            name="ck_session_review_policy",
        ),
    )
    session_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    paper_id: Mapped[UUID] = mapped_column(ForeignKey("papers.paper_id"), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    create_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    review_policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="IN_PROGRESS")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SessionTask(Base):
    __tablename__ = "session_tasks"
    __table_args__ = (
        UniqueConstraint("session_id", "paper_entry_id", name="uq_session_task_entry"),
        ForeignKeyConstraint(
            ["session_id", "paper_id"],
            ["assessment_sessions.session_id", "assessment_sessions.paper_id"],
            name="fk_session_task_session_paper",
        ),
        ForeignKeyConstraint(
            ["paper_entry_id", "paper_id"],
            ["paper_entries.paper_entry_id", "paper_entries.paper_id"],
            name="fk_session_task_entry_paper",
        ),
        ForeignKeyConstraint(
            ["current_attempt_id", "session_task_id"],
            ["attempts.attempt_id", "attempts.session_task_id"],
            name="fk_session_task_current_attempt",
            use_alter=True,
        ),
    )
    session_task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    paper_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    paper_entry_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    current_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("attempt_id", "session_task_id", name="uq_attempt_task"),
        UniqueConstraint("session_task_id", "redo_request_id", name="uq_attempt_redo_request"),
        CheckConstraint("status IN ('OPEN', 'SEALED', 'ABANDONED')", name="ck_attempt_status"),
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("session_tasks.session_task_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="OPEN")
    redo_request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _transaction(session: Session) -> None:
    if session.connection().get_isolation_level() != "READ COMMITTED":
        raise ValueError("Session operations require READ COMMITTED isolation")


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty operation identity is required")


def create_session(
    session: Session,
    *,
    paper_id: UUID,
    scope: str,
    create_request_id: str,
    review_policy: dict,
    store: AudioStore,
    registry: Registry = PRODUCTION_REGISTRY,
) -> AssessmentSession:
    """Create atomically; scope must be supplied by an already authorized internal caller."""
    _transaction(session)
    _identity(scope)
    _identity(create_request_id)
    try:
        policy = json.loads(json.dumps(review_policy, allow_nan=False))
        if not isinstance(policy, dict) or not policy:
            raise TypeError
    except (TypeError, ValueError) as exc:
        raise ValueError("Review policy must be a nonempty JSON object") from exc
    # Serialize the same scoped request before checking paper readiness. A unique
    # constraint is the durable backstop; the lock also makes retries after archive safe.
    key = json.dumps([scope, create_request_id], ensure_ascii=True, separators=(",", ":"))
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
    existing = session.scalar(
        select(AssessmentSession).where(
            AssessmentSession.scope == scope,
            AssessmentSession.create_request_id == create_request_id,
        )
    )
    if existing is not None:
        if existing.paper_id != paper_id or existing.review_policy != policy:
            raise ValueError("Create request conflicts with existing session")
        return existing
    paper = session.scalar(
        select(Paper)
        .where(Paper.paper_id == paper_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if paper is None or paper.status != "PUBLISHED":
        raise ValueError("Paper is not published")
    if registry.resolve(paper.scoring_plugin_id, paper.scoring_plugin_version) is None:
        raise ValueError("Scoring plugin is unavailable")
    if not readiness(session, paper_id, store).ready:
        raise ValueError("Paper execution contract or audio is not ready")
    rows = session.execute(
        select(PaperEntry, TaskRevision, EvaluationPlan)
        .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
        .join(EvaluationPlan, EvaluationPlan.paper_entry_id == PaperEntry.paper_entry_id)
        .where(PaperEntry.paper_id == paper_id)
        .order_by(PaperEntry.position)
    ).all()
    for entry, revision, plan in rows:
        declared = registry.requirements_for(
            paper.scoring_plugin_id, paper.scoring_plugin_version, revision.task_type
        )
        if (
            declared is None
            or plan.requirements != declared
            or any(
                not isinstance(req, dict) or not registry.supports(revision.task_type, req)
                for req in plan.requirements
            )
        ):
            raise ValueError("Frozen evaluation contract is unavailable or incompatible")
    result = AssessmentSession(
        session_id=uuid4(),
        paper_id=paper_id,
        scope=scope,
        create_request_id=create_request_id,
        review_policy=policy,
        status="IN_PROGRESS",
    )
    session.add(result)
    session.flush()
    session.add_all(
        [
            SessionTask(
                session_task_id=uuid4(),
                session_id=result.session_id,
                paper_id=paper_id,
                paper_entry_id=entry.paper_entry_id,
            )
            for entry, _, _ in rows
        ]
    )
    session.flush()
    return result


def ordered_tasks(session: Session, session_id: UUID) -> list[SessionTask]:
    return list(
        session.scalars(
            select(SessionTask)
            .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
            .where(SessionTask.session_id == session_id)
            .order_by(PaperEntry.position)
        )
    )


def _lock_task(session: Session, session_id: UUID, task_id: UUID) -> SessionTask:
    _transaction(session)
    owner = session.scalar(
        select(AssessmentSession)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner is None or owner.status != "IN_PROGRESS":
        raise ValueError("Session is not in progress")
    task = session.scalar(
        select(SessionTask)
        .where(SessionTask.session_id == session_id, SessionTask.session_task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise ValueError("Session task does not exist")
    return task


def start_attempt(session: Session, *, session_id: UUID, session_task_id: UUID) -> Attempt:
    task = _lock_task(session, session_id, session_task_id)
    if task.current_attempt_id is not None:
        return session.get(Attempt, task.current_attempt_id)
    attempt = Attempt(attempt_id=uuid4(), session_task_id=task.session_task_id, status="OPEN")
    session.add(attempt)
    session.flush()
    task.current_attempt_id = attempt.attempt_id
    session.flush()
    return attempt


def redo_attempt(
    session: Session, *, session_id: UUID, session_task_id: UUID, redo_request_id: str
) -> Attempt:
    _identity(redo_request_id)
    task = _lock_task(session, session_id, session_task_id)
    existing = session.scalar(
        select(Attempt).where(
            Attempt.session_task_id == task.session_task_id,
            Attempt.redo_request_id == redo_request_id,
        )
    )
    if existing is not None:
        return existing
    if task.current_attempt_id is not None:
        current = session.scalar(
            select(Attempt)
            .where(Attempt.attempt_id == task.current_attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current.status == "OPEN":
            current.status = "ABANDONED"
    attempt = Attempt(
        attempt_id=uuid4(),
        session_task_id=task.session_task_id,
        status="OPEN",
        redo_request_id=redo_request_id,
    )
    session.add(attempt)
    session.flush()
    task.current_attempt_id = attempt.attempt_id
    session.flush()
    return attempt
