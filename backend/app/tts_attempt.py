"""Durable technical execution records for frozen audio requests."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TTSAttempt(Base):
    __tablename__ = "tts_attempts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('SENDING','SAVED','DONE','FAILED','UNKNOWN')", name="ck_tts_attempt_state"
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    audio_id: Mapped[UUID] = mapped_column(
        ForeignKey("fixed_audio_requests.audio_id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    error_category: Mapped[str | None] = mapped_column(Text)
    retry_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    storage_ref: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
