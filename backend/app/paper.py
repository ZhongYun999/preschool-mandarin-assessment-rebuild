"""Draft papers: immutable revision references and optimistic edit protection.

Services use the caller's READ COMMITTED transaction. Callers must commit or roll
back the complete edit and supply the version they observed before editing.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.taskbank import TaskIdentity, TaskRevision


class Paper(Base):
    __tablename__ = "papers"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')", name="ck_paper_status"),
        CheckConstraint(
            "(scoring_plugin_id IS NULL) = (scoring_plugin_version IS NULL)",
            name="ck_paper_plugin_pair",
        ),
    )
    paper_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    bank_id: Mapped[UUID] = mapped_column(ForeignKey("task_banks.bank_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")
    scoring_plugin_id: Mapped[str | None] = mapped_column(Text)
    scoring_plugin_version: Mapped[str | None] = mapped_column(Text)
    edit_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PaperEntry(Base):
    __tablename__ = "paper_entries"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "position",
            name="uq_paper_entry_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    paper_entry_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    paper_id: Mapped[UUID] = mapped_column(ForeignKey("papers.paper_id"), nullable=False)
    task_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_revisions.task_revision_id"), nullable=False
    )
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EvaluationPlan(Base):
    __tablename__ = "evaluation_plans"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(requirements) = 'array'", name="ck_evaluation_requirements_array"
        ),
    )
    paper_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("paper_entries.paper_entry_id"), primary_key=True
    )
    task_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_revisions.task_revision_id"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[list] = mapped_column(JSONB, nullable=False)


class PaperEditConflict(ValueError):
    """The draft changed since it was read."""


def create_draft(
    session: Session, *, bank_id: UUID, name: str, description: str | None = None
) -> Paper:
    paper = Paper(
        paper_id=uuid4(),
        bank_id=bank_id,
        name=name,
        description=description,
        status="DRAFT",
        edit_version=0,
    )
    session.add(paper)
    session.flush()
    return paper


def _lock_draft(session: Session, paper_id: UUID, expected_version: int) -> Paper:
    if session.connection().get_isolation_level() != "READ COMMITTED":
        raise ValueError("Draft editing requires READ COMMITTED isolation")
    paper = session.scalar(
        select(Paper)
        .where(Paper.paper_id == paper_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if paper is None:
        raise ValueError("Paper does not exist")
    if paper.status != "DRAFT":
        raise ValueError("Paper is not a draft")
    if paper.edit_version != expected_version:
        raise PaperEditConflict("Paper has changed; reload before editing")
    return paper


def _latest(session: Session, paper: Paper, task_id: UUID) -> UUID:
    # The importer takes the same identity lock before appending a revision.
    task = session.scalar(
        select(TaskIdentity)
        .where(TaskIdentity.task_id == task_id, TaskIdentity.bank_id == paper.bank_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise ValueError("Task is not in the paper's bank")
    revision_id = session.scalar(
        select(TaskRevision.task_revision_id)
        .where(TaskRevision.task_id == task_id)
        .order_by(TaskRevision.version.desc())
        .limit(1)
    )
    if revision_id is None:
        raise ValueError("Task has no revision")
    return revision_id


def _entry(session: Session, paper_id: UUID, entry_id: UUID) -> PaperEntry:
    entry = session.scalar(
        select(PaperEntry)
        .where(PaperEntry.paper_id == paper_id, PaperEntry.paper_entry_id == entry_id)
        .execution_options(populate_existing=True)
    )
    if entry is None:
        raise ValueError("Paper entry does not exist")
    return entry


def update_draft(
    session: Session, *, paper_id: UUID, expected_version: int, name: str, description: str | None
) -> None:
    paper = _lock_draft(session, paper_id, expected_version)
    paper.name, paper.description = name, description
    paper.edit_version += 1
    session.flush()


def add_entry(
    session: Session, *, paper_id: UUID, expected_version: int, task_id: UUID, position: int
) -> PaperEntry:
    paper = _lock_draft(session, paper_id, expected_version)
    entry = PaperEntry(
        paper_entry_id=uuid4(),
        paper_id=paper_id,
        task_revision_id=_latest(session, paper, task_id),
        position=position,
    )
    session.add(entry)
    paper.edit_version += 1
    session.flush()
    return entry


def remove_entry(
    session: Session, *, paper_id: UUID, expected_version: int, entry_id: UUID
) -> None:
    paper = _lock_draft(session, paper_id, expected_version)
    entry = _entry(session, paper_id, entry_id)
    from app.fixed_audio import FixedAudio

    for audio in session.scalars(select(FixedAudio).where(FixedAudio.paper_entry_id == entry_id)):
        session.delete(audio)
    session.flush()
    plan = session.get(EvaluationPlan, entry_id)
    if plan is not None:
        session.delete(plan)
        session.flush()
    session.delete(entry)
    paper.edit_version += 1
    session.flush()


def replace_entry(
    session: Session, *, paper_id: UUID, expected_version: int, entry_id: UUID, task_id: UUID
) -> None:
    paper = _lock_draft(session, paper_id, expected_version)
    entry = _entry(session, paper_id, entry_id)
    from app.fixed_audio import FixedAudio

    for audio in session.scalars(select(FixedAudio).where(FixedAudio.paper_entry_id == entry_id)):
        session.delete(audio)
    session.flush()
    plan = session.get(EvaluationPlan, entry_id)
    if plan is not None:
        session.delete(plan)
        session.flush()
    entry.task_revision_id = _latest(session, paper, task_id)
    paper.edit_version += 1
    session.flush()


def reorder_entries(
    session: Session, *, paper_id: UUID, expected_version: int, entry_ids: list[UUID]
) -> None:
    paper = _lock_draft(session, paper_id, expected_version)
    entries = session.scalars(
        select(PaperEntry)
        .where(PaperEntry.paper_id == paper_id)
        .execution_options(populate_existing=True)
    ).all()
    by_id = {entry.paper_entry_id: entry for entry in entries}
    if len(entry_ids) != len(by_id) or set(entry_ids) != set(by_id):
        raise ValueError("Order must contain each entry exactly once")
    for position, entry_id in enumerate(entry_ids, 1):
        by_id[entry_id].position = position
    paper.edit_version += 1
    session.flush()


def copy_paper(session: Session, *, source_id: UUID) -> Paper:
    from app.fixed_audio import FixedAudio

    if session.connection().get_isolation_level() != "READ COMMITTED":
        raise ValueError("Paper copying requires READ COMMITTED isolation")
    source = session.scalar(
        select(Paper)
        .where(Paper.paper_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source is None:
        raise ValueError("Paper does not exist")
    copied = Paper(
        paper_id=uuid4(),
        bank_id=source.bank_id,
        name=source.name,
        description=source.description,
        status="DRAFT",
        scoring_plugin_id=source.scoring_plugin_id,
        scoring_plugin_version=source.scoring_plugin_version,
        edit_version=0,
    )
    session.add(copied)
    session.flush()
    entries = session.scalars(
        select(PaperEntry).where(PaperEntry.paper_id == source_id).order_by(PaperEntry.position)
    ).all()
    for e in entries:
        new_entry = PaperEntry(
            paper_entry_id=uuid4(),
            paper_id=copied.paper_id,
            task_revision_id=e.task_revision_id,
            position=e.position,
        )
        session.add(new_entry)
        session.flush()
        plan = session.get(EvaluationPlan, e.paper_entry_id)
        if plan is not None:
            session.add(
                EvaluationPlan(
                    paper_entry_id=new_entry.paper_entry_id,
                    task_revision_id=plan.task_revision_id,
                    plugin_id=plan.plugin_id,
                    plugin_version=plan.plugin_version,
                    requirements=plan.requirements,
                )
            )
            session.flush()
        for audio in session.scalars(
            select(FixedAudio).where(FixedAudio.paper_entry_id == e.paper_entry_id)
        ):
            session.add(
                FixedAudio(
                    audio_id=uuid4(),
                    paper_entry_id=new_entry.paper_entry_id,
                    task_revision_id=audio.task_revision_id,
                    role=audio.role,
                    source_text=audio.source_text,
                    text_sha256=audio.text_sha256,
                    config=audio.config,
                    status="PENDING",
                )
            )
        session.flush()
    return copied
