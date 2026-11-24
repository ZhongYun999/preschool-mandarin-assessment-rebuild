"""Speaking checkpoint A facts and recording operations."""

import hashlib
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


def replay_target(db, task, attempt_id):
    """Derive the current replay target without falling back from a missing newer resource."""
    from app.media.fixed_audio import FixedAudio
    from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest

    help_row = db.scalar(
        select(SpeakingHelpRequest)
        .where(
            SpeakingHelpRequest.attempt_id == attempt_id, SpeakingHelpRequest.status == "DELIVERED"
        )
        .order_by(SpeakingHelpRequest.position.desc())
        .limit(1)
    )
    if help_row is not None:
        audio = db.scalar(
            select(GuidanceAudio).where(
                GuidanceAudio.help_request_id == help_row.help_request_id,
                GuidanceAudio.attempt_id == attempt_id,
            )
        )
        return "GUIDANCE", audio, help_row
    audio = db.scalar(
        select(FixedAudio).where(
            FixedAudio.paper_entry_id == task.paper_entry_id, FixedAudio.role == "prompt"
        )
    )
    return "INITIAL_PROMPT", audio, None


def replay_resource_available(audio, store):
    if audio is None or audio.status != "READY":
        return False
    try:
        store.read(audio.storage_ref, audio.content_sha256)
    except (ValueError, OSError):
        return False
    return True


def replay_allowed(db, attempt_id, revision):
    from app.task_types.speaking_guidance import active_help

    return not (
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
    )


def replay_frozen_resource(db, task, attempt_id, playback, store):
    from app.media.fixed_audio import FixedAudio
    from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest

    if playback.audio_id is not None:
        audio = db.get(FixedAudio, playback.audio_id)
        if audio is None or audio.paper_entry_id != task.paper_entry_id or audio.role != "prompt":
            raise ValueError("Replay resource identity mismatch")
    else:
        audio = db.get(GuidanceAudio, playback.guidance_audio_id)
        help_row = db.get(SpeakingHelpRequest, audio.help_request_id) if audio else None
        if (
            audio is None
            or audio.attempt_id != attempt_id
            or help_row is None
            or help_row.attempt_id != attempt_id
            or help_row.status != "DELIVERED"
            or not help_row.guidance_text
            or hashlib.sha256(help_row.guidance_text.encode("utf-8")).hexdigest()
            != audio.text_sha256
        ):
            raise ValueError("Replay resource identity mismatch")
    if not replay_resource_available(audio, store):
        raise ValueError("Replay audio unavailable")
    return audio


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
