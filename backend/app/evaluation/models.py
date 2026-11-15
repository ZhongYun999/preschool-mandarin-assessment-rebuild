"""Persistent identities for frozen evaluation requests and raw evidence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_evaluation_job_intent"),
        UniqueConstraint("job_id", "submission_id", name="uq_evaluation_job_submission"),
        ForeignKeyConstraint(
            ["intent_id", "submission_id", "paper_entry_id", "requirement_index"],
            [
                "evaluation_intents.intent_id",
                "evaluation_intents.submission_id",
                "evaluation_intents.paper_entry_id",
                "evaluation_intents.requirement_index",
            ],
            name="fk_job_intent_identity",
        ),
        CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN')",
            name="ck_evaluation_job_status",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR (status <> 'RUNNING' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_evaluation_job_lease",
        ),
        CheckConstraint(
            "(input_refs IS NULL) = (input_fingerprint IS NULL)",
            name="ck_evaluation_job_input_pair",
        ),
        CheckConstraint(
            "status = 'WAITING' OR input_fingerprint IS NOT NULL",
            name="ck_evaluation_job_claim_input",
        ),
        CheckConstraint(
            "jsonb_typeof(requirement) = 'object'", name="ck_evaluation_job_requirement"
        ),
        CheckConstraint(
            "input_refs IS NULL OR jsonb_typeof(input_refs) = 'object'",
            name="ck_evaluation_job_input_refs",
        ),
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    paper_entry_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    requirement_index: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="WAITING")
    claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_refs: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    input_fingerprint: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RawEvaluation(Base):
    __tablename__ = "raw_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.job_id", "evaluation_jobs.submission_id"],
            name="fk_raw_job_submission",
        ),
        UniqueConstraint("execution_id", name="uq_raw_execution"),
        CheckConstraint(
            "status IN ('READY','FAILED','INVALID','UNKNOWN')", name="ck_raw_evaluation_status"
        ),
        CheckConstraint(
            "(status = 'READY' AND payload IS NOT NULL AND error_code IS NULL) OR (status <> 'READY' AND payload IS NULL AND error_code IS NOT NULL)",
            name="ck_raw_evaluation_payload",
        ),
        CheckConstraint("jsonb_typeof(input_refs) = 'object'", name="ck_raw_evaluation_refs"),
        Index(
            "uq_raw_evaluation_ready_job",
            "job_id",
            unique=True,
            postgresql_where=text("status = 'READY'"),
        ),
    )
    evaluation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_refs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
