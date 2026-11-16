"""Persistent recording operations and immutable saved audio metadata."""

from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (
        UniqueConstraint("attempt_id", "request_id", name="uq_recording_request"),
        CheckConstraint("status IN ('STARTED', 'SAVED', 'CANCELLED')", name="ck_recording_status"),
        CheckConstraint(
            "(status = 'SAVED' AND storage_ref IS NOT NULL AND sha256 IS NOT NULL AND media_type IS NOT NULL) OR "
            "(status <> 'SAVED' AND storage_ref IS NULL AND sha256 IS NULL AND media_type IS NULL)",
            name="ck_recording_metadata",
        ),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="ck_recording_sha256"),
        Index(
            "uq_recording_active_attempt",
            "attempt_id",
            unique=True,
            postgresql_where=text("status = 'STARTED'"),
        ),
    )
    recording_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    storage_ref: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)


def recording_busy(session: Session, attempt_id: UUID) -> bool:
    return (
        session.scalar(
            select(Recording.recording_id)
            .where(Recording.attempt_id == attempt_id, Recording.status == "STARTED")
            .limit(1)
        )
        is not None
    )
