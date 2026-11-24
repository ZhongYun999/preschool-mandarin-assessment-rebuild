"""Speaking checkpoint A facts and recording operations."""

from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base
from app.media.recording import Recording, recording_busy
from app.sessions.assessment_session import _identity
from app.sessions.submission import _busy, _completed, _locked, _open, _revision
from app.task_types.simple_recording import _recording
from app.task_types.speaking_asr import SpeakingASR


class SpeakingSegment(Base):
    __tablename__ = "speaking_segments"
    __table_args__ = (
        UniqueConstraint("attempt_id", "position", name="uq_speaking_segment_position"),
        UniqueConstraint("recording_id", name="uq_speaking_segment_recording"),
        CheckConstraint("position > 0", name="ck_speaking_segment_position"),
        CheckConstraint("duration_seconds > 0", name="ck_speaking_segment_duration"),
    )
    segment_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    recording_id: Mapped[UUID] = mapped_column(
        ForeignKey("recordings.recording_id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)


def current(db, session_id, task_id, attempt_id):
    task, attempt = _locked(db, session_id, task_id, attempt_id)
    _open(task, attempt)
    revision = _revision(db, task)
    if revision.task_type != "speaking":
        raise ValueError("Unsupported task type")
    return revision


def start(db: Session, *, session_id, session_task_id, attempt_id, request_id):
    from app.task_types.speaking_guidance import active_help

    _identity(request_id)
    revision = current(db, session_id, session_task_id, attempt_id)
    previous = db.scalar(
        select(Recording).where(
            Recording.attempt_id == attempt_id, Recording.request_id == request_id
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
        or db.scalar(
            select(SpeakingSegment.segment_id)
            .outerjoin(SpeakingASR, SpeakingASR.segment_id == SpeakingSegment.segment_id)
            .where(
                SpeakingSegment.attempt_id == attempt_id,
                (SpeakingASR.status.is_(None) | (SpeakingASR.status != "READY")),
            )
            .limit(1)
        )
        is not None
    ):
        raise ValueError("Recording is not allowed in this stage")
    row = Recording(
        recording_id=uuid4(), attempt_id=attempt_id, request_id=request_id, status="STARTED"
    )
    db.add(row)
    db.flush()
    return row


def cancel(db, *, session_id, session_task_id, attempt_id, recording_id):
    current(db, session_id, session_task_id, attempt_id)
    row = _recording(db, attempt_id, recording_id)
    if row.status == "SAVED":
        raise ValueError("Saved recording cannot be cancelled")
    if row.status == "STARTED":
        row.status = "CANCELLED"
        db.flush()
    return row


def save(
    db,
    *,
    session_id,
    session_task_id,
    attempt_id,
    recording_id,
    storage_ref,
    sha256,
    media_type,
    store,
    duration_seconds,
):
    current(db, session_id, session_task_id, attempt_id)
    row = _recording(db, attempt_id, recording_id)
    if row.status == "SAVED":
        if (row.storage_ref, row.sha256, row.media_type) != (storage_ref, sha256, media_type):
            raise ValueError("Recording upload conflicts")
        store.verify(row.storage_ref, row.sha256)
        return row
    if row.status != "STARTED" or _busy(db, attempt_id):
        raise ValueError("Recording is not active")
    store.verify(storage_ref, sha256)
    positions = list(
        db.scalars(select(SpeakingSegment.position).where(SpeakingSegment.attempt_id == attempt_id))
    )
    row.status, row.storage_ref, row.sha256, row.media_type = (
        "SAVED",
        storage_ref,
        sha256,
        media_type,
    )
    db.flush()
    segment = SpeakingSegment(
        segment_id=uuid4(),
        attempt_id=attempt_id,
        recording_id=recording_id,
        position=max(positions, default=0) + 1,
        duration_seconds=duration_seconds,
    )
    db.add(segment)
    db.flush()
    db.add(
        SpeakingASR(
            segment_id=segment.segment_id,
            attempt_id=attempt_id,
            recording_id=recording_id,
            sha256=sha256,
            status="WAITING",
            attempts=0,
        )
    )
    db.flush()
    return row
