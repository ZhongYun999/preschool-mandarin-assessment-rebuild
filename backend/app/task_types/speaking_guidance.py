"""Speaking help requests and their frozen input frontier."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.media.recording import recording_busy
from app.sessions.assessment_session import _identity
from app.sessions.submission import _busy, _completed
from app.task_types.speaking import SpeakingSegment, current
from app.task_types.speaking_asr import SpeakingASR


class SpeakingHelpRequest(Base):
    __tablename__ = "speaking_help_requests"
    __table_args__ = (
        UniqueConstraint("attempt_id", "request_id", name="uq_speaking_help_request"),
        Index(
            "uq_speaking_help_active",
            "attempt_id",
            unique=True,
            postgresql_where=text("status <> 'VOIDED'"),
        ),
        CheckConstraint("source = 'child_requested'", name="ck_speaking_help_source"),
        CheckConstraint(
            "jsonb_typeof(execution_history) = 'array'", name="ck_speaking_help_history"
        ),
        CheckConstraint(
            "status IN ('WAITING','RUNNING','FAILED','UNKNOWN','READY','VOIDED')",
            name="ck_speaking_help_status",
        ),
        CheckConstraint(
            "(status = 'READY' AND guidance_text IS NOT NULL AND length(trim(guidance_text)) > 0) OR (status <> 'READY' AND guidance_text IS NULL)",
            name="ck_speaking_help_text",
        ),
    )
    help_request_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="child_requested")
    input_frontier: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="WAITING")
    guidance_text: Mapped[str | None] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(Text)
    contract: Mapped[dict | None] = mapped_column(JSONB)
    claim_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    retry_request_id: Mapped[str | None] = mapped_column(Text)
    retry_expected_attempts: Mapped[int | None] = mapped_column(Integer)


def active_help(db, attempt_id):
    return db.scalar(
        select(SpeakingHelpRequest)
        .where(
            SpeakingHelpRequest.attempt_id == attempt_id,
            SpeakingHelpRequest.status != "VOIDED",
        )
        .order_by(SpeakingHelpRequest.help_request_id)
        .limit(1)
    )


def request_help(db, *, session_id, session_task_id, attempt_id, request_id):
    _identity(request_id)
    revision = current(db, session_id, session_task_id, attempt_id)
    previous = db.scalar(
        select(SpeakingHelpRequest).where(
            SpeakingHelpRequest.attempt_id == attempt_id,
            SpeakingHelpRequest.request_id == request_id,
        )
    )
    if previous is not None:
        return previous
    if (
        (revision.guide_text and not _completed(db, attempt_id, "GUIDE"))
        or not _completed(db, attempt_id, "INITIAL_PROMPT")
        or _busy(db, attempt_id)
        or recording_busy(db, attempt_id)
        or active_help(db, attempt_id) is not None
    ):
        raise ValueError("Help is not allowed in this stage")
    segments = list(
        db.scalars(
            select(SpeakingSegment)
            .where(SpeakingSegment.attempt_id == attempt_id)
            .order_by(SpeakingSegment.position)
        )
    )
    frontier = []
    for segment in segments:
        asr = db.scalar(select(SpeakingASR).where(SpeakingASR.segment_id == segment.segment_id))
        if asr is None or asr.status != "READY":
            raise ValueError("Recognition is not complete")
        frontier.append(
            {
                "segment_id": str(segment.segment_id),
                "asr_operation_id": str(asr.operation_id),
                "semantic": asr.semantic,
                "text": asr.transcript,
            }
        )
    row = SpeakingHelpRequest(
        help_request_id=uuid4(),
        attempt_id=attempt_id,
        request_id=request_id,
        input_frontier={
            "task_revision_id": str(revision.task_revision_id),
            "segments": frontier,
            "delivered_guidance": [],
        },
        status="WAITING",
        source="child_requested",
        attempts=0,
        execution_history=[],
    )
    db.add(row)
    db.flush()
    return row
