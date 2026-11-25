"""Durable per-segment operations for formal Speaking ASR."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpeakingFormalASROperation(Base):
    __tablename__ = "speaking_formal_asr_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.job_id", "evaluation_jobs.submission_id"],
            name="fk_formal_asr_job_submission",
        ),
        ForeignKeyConstraint(
            ["segment_id"], ["speaking_segments.segment_id"], name="fk_formal_asr_segment"
        ),
        ForeignKeyConstraint(
            ["recording_id"], ["recordings.recording_id"], name="fk_formal_asr_recording"
        ),
        UniqueConstraint("job_id", "segment_id", name="uq_formal_asr_job_segment"),
        UniqueConstraint("job_id", "position", name="uq_formal_asr_job_position"),
        CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN')",
            name="ck_formal_asr_operation_status",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR (status <> 'RUNNING' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_formal_asr_operation_lease",
        ),
        CheckConstraint(
            "(status = 'READY') = (result IS NOT NULL)", name="ck_formal_asr_operation_result"
        ),
    )
    operation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    submission_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    segment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    recording_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    audio_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="WAITING")
    claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SpeakingFormalASRAttempt(Base):
    __tablename__ = "speaking_formal_asr_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id"],
            ["speaking_formal_asr_operations.operation_id"],
            name="fk_formal_asr_attempt_operation",
        ),
        CheckConstraint(
            "status IN ('RUNNING','READY','FAILED','INVALID','UNKNOWN','UNDISPATCHED')",
            name="ck_formal_asr_attempt_status",
        ),
    )
    execution_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
