"""Task-bank identity, immutable revisions, and their image references."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base


class TaskBank(Base):
    __tablename__ = "task_banks"
    bank_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class TaskIdentity(Base):
    __tablename__ = "task_identities"
    __table_args__ = (UniqueConstraint("bank_id", "task_code", name="uq_bank_task_code"),)
    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    bank_id: Mapped[UUID] = mapped_column(ForeignKey("task_banks.bank_id"), nullable=False)
    task_code: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int | None] = mapped_column(BigInteger)


class TaskRevision(Base):
    __tablename__ = "task_revisions"
    __table_args__ = (
        UniqueConstraint("task_id", "version", name="uq_task_revision_version"),
        CheckConstraint("version > 0", name="ck_revision_version_positive"),
        CheckConstraint(
            "(target_rounds IS NULL OR target_rounds > 0) AND "
            "(task_type <> 'dialogue' OR target_rounds IS NOT NULL)",
            name="ck_dialogue_target_rounds",
        ),
    )
    task_revision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task_identities.task_id"), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grade_level: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    guide_text: Mapped[str | None] = mapped_column(Text)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_rounds: Mapped[int | None] = mapped_column(BigInteger)
    material_description: Mapped[str | None] = mapped_column(Text)
    reference_answer: Mapped[str | None] = mapped_column(Text)
    scoring_instruction: Mapped[str | None] = mapped_column(Text)
    ai_followup_scope: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_asset_sha256"),)
    asset_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)


class TaskRevisionAsset(Base):
    __tablename__ = "task_revision_assets"
    __table_args__ = (
        CheckConstraint(
            "slot IN ('option_A', 'option_B', 'option_C', 'question_image')",
            name="ck_revision_asset_slot",
        ),
    )
    task_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_revisions.task_revision_id"), primary_key=True
    )
    slot: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.asset_id"), nullable=False, index=True)


@dataclass(frozen=True)
class RevisionContent:
    """Input is already normalized by the caller; no text rewriting is performed."""
    grade_level: str
    task_type: str
    prompt_text: str
    guide_text: str | None = None
    target_rounds: int | None = None
    material_description: str | None = None
    reference_answer: str | None = None
    scoring_instruction: str | None = None
    ai_followup_scope: str | None = None


@dataclass(frozen=True)
class AppendResult:
    task_id: UUID
    task_revision_id: UUID
    version: int
    created: bool


def append_revision(
    engine: Engine, *, bank_id: UUID, task_code: str, display_order: int | None,
    content: RevisionContent, images: dict[str, UUID],
) -> AppendResult:
    """Commit identity, comparison, revision, image links and order as one transaction.

    PostgreSQL READ COMMITTED is required: conflict handling and the subsequent
    row lock must observe the preceding writer's committed revision.
    """
    slots = {"option_A", "option_B", "option_C", "question_image"}
    if not images.keys() <= slots:
        raise ValueError("Unknown image slot")
    with Session(engine) as session, session.begin():
        isolation = session.connection().get_isolation_level()
        if isolation != "READ COMMITTED":
            raise ValueError("append_revision requires READ COMMITTED isolation")
        session.execute(
            insert(TaskIdentity).values(
                task_id=uuid4(), bank_id=bank_id, task_code=task_code, display_order=display_order
            ).on_conflict_do_nothing(index_elements=["bank_id", "task_code"])
        )
        task = session.scalar(
            select(TaskIdentity).where(
                TaskIdentity.bank_id == bank_id, TaskIdentity.task_code == task_code
            ).with_for_update()
        )
        task.display_order = display_order
        assets = {}
        for asset_id in sorted(set(images.values()), key=str):
            asset = session.scalar(select(Asset).where(Asset.asset_id == asset_id).with_for_update())
            if asset is None:
                raise ValueError("Image asset does not exist")
            assets[asset_id] = asset
        payload = {
            "content": asdict(content),
            "images": {slot: assets[asset_id].sha256 for slot, asset_id in images.items()},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = sha256(encoded.encode("utf-8")).hexdigest()
        latest = session.scalar(
            select(TaskRevision).where(TaskRevision.task_id == task.task_id)
            .order_by(TaskRevision.version.desc()).limit(1)
        )
        if latest is not None and latest.content_hash == fingerprint:
            return AppendResult(task.task_id, latest.task_revision_id, latest.version, False)
        revision = TaskRevision(
            task_revision_id=uuid4(), task_id=task.task_id,
            version=1 if latest is None else latest.version + 1,
            content_hash=fingerprint, **asdict(content),
        )
        session.add(revision)
        session.flush()
        session.add_all(
            TaskRevisionAsset(task_revision_id=revision.task_revision_id, slot=slot, asset_id=asset_id)
            for slot, asset_id in images.items()
        )
        session.flush()
        return AppendResult(task.task_id, revision.task_revision_id, revision.version, True)
