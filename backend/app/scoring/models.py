"""Immutable automatic scores and first formal result."""

from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AutomaticTaskScore(Base):
    __tablename__ = "automatic_task_scores"
    __table_args__ = (
        UniqueConstraint("manifest_id", "session_task_id", name="uq_auto_task_manifest_task"),
        ForeignKeyConstraint(
            ["manifest_id", "session_task_id"],
            ["close_manifest_entries.manifest_id", "close_manifest_entries.session_task_id"],
            name="fk_auto_task_manifest_entry",
        ),
    )
    score_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.submission_id"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
    raw_evaluation_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AutomaticPaperScore(Base):
    __tablename__ = "automatic_paper_scores"
    score_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_manifests.manifest_id"), nullable=False, unique=True
    )
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
    task_score_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PendingReview(Base):
    __tablename__ = "pending_score_reviews"
    __table_args__ = (
        UniqueConstraint("manifest_id", "session_task_id", name="uq_pending_review_task"),
        ForeignKeyConstraint(
            ["manifest_id", "session_task_id"],
            ["close_manifest_entries.manifest_id", "close_manifest_entries.session_task_id"],
            name="fk_review_manifest_entry",
        ),
    )
    review_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class FinalScoreResult(Base):
    __tablename__ = "final_score_results"
    result_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessment_sessions.session_id"), nullable=False, unique=True
    )
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_manifests.manifest_id"), nullable=False, unique=True
    )
    paper_score_id: Mapped[UUID] = mapped_column(
        ForeignKey("automatic_paper_scores.score_id"), nullable=False, unique=True
    )
    reviewed_paper_score_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("reviewed_paper_scores.score_id"), unique=True
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        UniqueConstraint("manifest_id", "revision", name="uq_review_decision_revision"),
        UniqueConstraint("manifest_id", "operation_id", name="uq_review_decision_operation"),
        ForeignKeyConstraint(
            ["manifest_id", "session_task_id"],
            ["close_manifest_entries.manifest_id", "close_manifest_entries.session_task_id"],
            name="fk_review_decision_manifest_entry",
        ),
        CheckConstraint("revision > 0", name="ck_review_revision_positive"),
        CheckConstraint(
            "(action='CONFIRM' AND override_value IS NULL AND effective_result IS NULL) OR (action='OVERRIDE' AND override_value IS NOT NULL AND effective_result IS NOT NULL)",
            name="ck_review_decision_action",
        ),
    )
    decision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.submission_id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    teacher_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    override_value: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))
    effective_result: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))


class ReviewedPaperScore(Base):
    __tablename__ = "reviewed_paper_scores"
    __table_args__ = (
        UniqueConstraint("manifest_id", "revision", name="uq_reviewed_paper_revision"),
        ForeignKeyConstraint(
            ["manifest_id", "revision"],
            ["review_decisions.manifest_id", "review_decisions.revision"],
            name="fk_reviewed_paper_decision_revision",
        ),
    )
    score_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_manifests.manifest_id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[list] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
