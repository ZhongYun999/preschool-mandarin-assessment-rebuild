"""Speaking checkpoint A facts and recording operations."""

import hashlib
import math
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


class SpeakingPolicy(Base):
    __tablename__ = "speaking_runtime_policy"
    __table_args__ = (
        CheckConstraint("policy_id = 1", name="ck_speaking_policy_singleton"),
        CheckConstraint(
            "minimum_duration_seconds > 0 AND minimum_duration_seconds < 'Infinity'::float8",
            name="ck_speaking_policy_duration",
        ),
        CheckConstraint("revision > 0", name="ck_speaking_policy_revision"),
    )
    policy_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    minimum_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


def finish_qualification(db, task, attempt, revision, store, *, lock_policy=False):
    """Read the same submission facts for projection and locked submission."""
    from app.sessions.assessment_session import AssessmentSession
    from app.task_types.speaking_guidance import active_help

    query = select(SpeakingPolicy).where(SpeakingPolicy.policy_id == 1)
    if lock_policy:
        query = query.with_for_update(read=True)
    policy = db.scalar(query.execution_options(populate_existing=True))
    segments = list(
        db.scalars(
            select(SpeakingSegment)
            .where(SpeakingSegment.attempt_id == attempt.attempt_id)
            .order_by(SpeakingSegment.position)
        )
    )
    total = math.fsum(segment.duration_seconds for segment in segments)
    minimum = policy.minimum_duration_seconds if policy else None
    minimum_met = bool(policy and total >= minimum)
    reason = None
    owner = db.get(AssessmentSession, task.session_id)
    if not policy or not math.isfinite(minimum) or minimum <= 0:
        reason = "SPEAKING_POLICY_UNAVAILABLE"
    elif (
        owner.status != "IN_PROGRESS"
        or task.current_attempt_id != attempt.attempt_id
        or attempt.status != "OPEN"
    ):
        reason = "ATTEMPT_NOT_OPEN"
    elif (
        revision.guide_text and not _completed(db, attempt.attempt_id, "GUIDE")
    ) or not _completed(db, attempt.attempt_id, "INITIAL_PROMPT"):
        reason = "INITIAL_DELIVERY_INCOMPLETE"
    elif (
        recording_busy(db, attempt.attempt_id)
        or _busy(db, attempt.attempt_id)
        or active_help(db, attempt.attempt_id) is not None
    ):
        reason = "OPERATION_IN_PROGRESS"
    elif not minimum_met:
        reason = "MINIMUM_DURATION_NOT_MET"
    manifest = []
    for segment in segments:
        recording = db.get(Recording, segment.recording_id)
        recognition = db.scalar(
            select(SpeakingASR).where(SpeakingASR.segment_id == segment.segment_id)
        )
        if (
            recording is None
            or recording.attempt_id != attempt.attempt_id
            or recording.status != "SAVED"
        ):
            reason = reason or "RECORDING_UNAVAILABLE"
            continue
        if (
            recognition is None
            or recognition.status != "READY"
            or recognition.attempt_id != attempt.attempt_id
            or recognition.recording_id != recording.recording_id
            or recognition.sha256 != recording.sha256
        ):
            reason = reason or "ASR_NOT_READY"
            continue
        try:
            store.verify(recording.storage_ref, recording.sha256)
        except (ValueError, OSError):
            reason = reason or "RECORDING_UNAVAILABLE"
            continue
        manifest.append(
            {
                "segment_id": str(segment.segment_id),
                "recording_id": str(recording.recording_id),
                "position": segment.position,
                "storage_ref": recording.storage_ref,
                "sha256": recording.sha256,
                "media_type": recording.media_type,
                "duration_seconds": segment.duration_seconds,
                "asr_operation_id": str(recognition.operation_id),
                "asr_status": recognition.status,
            }
        )
    return {
        "policy": policy,
        "total": total,
        "minimum": minimum,
        "minimum_met": minimum_met,
        "eligible": reason is None,
        "reason": reason,
        "segments": manifest,
    }


def submit_speaking(db, *, session_id, session_task_id, attempt_id, finish_request_id, store):
    from app.papers.paper import PaperEntry
    from app.sessions.submission import (
        Playback,
        Submission,
        _accept_submission,
        effective_replay_ids,
    )
    from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest

    _identity(finish_request_id)
    task, attempt = _locked(db, session_id, session_task_id, attempt_id, allow_closed=True)
    revision = _revision(db, task)
    if revision.task_type != "speaking":
        raise ValueError("Unsupported task type")
    existing = db.scalar(
        select(Submission).where(
            Submission.session_task_id == session_task_id,
            Submission.submit_request_id == finish_request_id,
        )
    )
    if existing:
        if existing.attempt_id != attempt_id:
            raise ValueError("Submission request conflicts")
        return existing
    qualification = finish_qualification(db, task, attempt, revision, store, lock_policy=True)
    if not qualification["eligible"]:
        raise ValueError(qualification["reason"])
    delivered = list(
        db.scalars(
            select(SpeakingHelpRequest)
            .where(
                SpeakingHelpRequest.attempt_id == attempt_id,
                SpeakingHelpRequest.status == "DELIVERED",
                SpeakingHelpRequest.source == "child_requested",
            )
            .order_by(SpeakingHelpRequest.position)
        )
    )
    help_facts = []
    for item in delivered:
        audio = db.scalar(
            select(GuidanceAudio).where(
                GuidanceAudio.help_request_id == item.help_request_id,
                GuidanceAudio.attempt_id == attempt_id,
            )
        )
        rows = (
            list(
                db.scalars(
                    select(Playback).where(
                        Playback.attempt_id == attempt_id,
                        Playback.guidance_audio_id == audio.audio_id,
                        Playback.status == "COMPLETED",
                    )
                )
            )
            if audio
            else []
        )
        delivery = next(
            (
                p
                for p in rows
                if p.kind == "GUIDANCE"
                or (
                    p.kind == "TECHNICAL_RETRY"
                    and (original := db.get(Playback, p.retry_of_id)) is not None
                    and original.kind == "GUIDANCE"
                    and original.status == "FAILED"
                    and original.attempt_id == attempt_id
                    and original.guidance_audio_id == audio.audio_id
                )
            ),
            None,
        )
        if delivery is None:
            raise ValueError("HELP_DELIVERY_EVIDENCE_UNAVAILABLE")
        help_facts.append(
            {
                "help_request_id": str(item.help_request_id),
                "position": item.position,
                "guidance_audio_id": str(audio.audio_id),
                "delivery_playback_id": str(delivery.playback_id),
            }
        )
    replay_ids = effective_replay_ids(db, attempt_id)
    policy = qualification["policy"]
    entry = db.get(PaperEntry, task.paper_entry_id)
    return _accept_submission(
        db,
        task=task,
        attempt=attempt,
        request_id=finish_request_id,
        inputs={
            "schema": "speaking-input-v1",
            "paper_entry_id": str(task.paper_entry_id),
            "task_revision_id": str(entry.task_revision_id),
            "segments": qualification["segments"],
            "total_duration_seconds": qualification["total"],
            "minimum_duration_seconds": qualification["minimum"],
            "policy_revision": policy.revision,
            "delivered_help": help_facts,
            "effective_help_count": len(help_facts),
            "replay_playback_ids": [str(value) for value in replay_ids],
            "replay_count": len(replay_ids),
        },
    )


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
