"""Process ASR identities for Speaking segments, independent of formal evaluation."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpeakingASR(Base):
    __tablename__ = "speaking_asr_operations"
    __table_args__ = (
        UniqueConstraint("segment_id", name="uq_speaking_asr_segment"),
        CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','UNKNOWN')",
            name="ck_speaking_asr_status",
        ),
        CheckConstraint(
            "semantic IS NULL OR semantic IN ('TRANSCRIBED','NO_RECOGNIZED_SPEECH')",
            name="ck_speaking_asr_semantic",
        ),
        CheckConstraint(
            "(status <> 'READY' AND semantic IS NULL AND transcript IS NULL) OR "
            "(status = 'READY' AND semantic IS NOT NULL AND "
            "((semantic = 'TRANSCRIBED' AND transcript IS NOT NULL AND length(trim(transcript)) > 0) "
            "OR (semantic = 'NO_RECOGNIZED_SPEECH' AND transcript IS NULL)))",
            name="ck_speaking_asr_result",
        ),
    )
    operation_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("speaking_segments.segment_id"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    recording_id: Mapped[UUID] = mapped_column(
        ForeignKey("recordings.recording_id"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="WAITING")
    contract: Mapped[dict | None] = mapped_column(JSONB)
    claim_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    retry_request_id: Mapped[str | None] = mapped_column(Text)
    retry_expected_attempts: Mapped[int | None] = mapped_column(Integer)
