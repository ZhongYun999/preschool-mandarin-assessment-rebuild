"""Resolve and validate frozen recording and Speaking source identities."""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.identity import canonical_json as _json
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import PaperEntry
from app.sessions.assessment_session import Attempt, SessionTask
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision


def recording_input(session: Session, submission: Submission) -> dict:
    """Resolve the frozen recording identity; adapters verify bytes outside the transaction."""
    try:
        recording_id = UUID(submission.inputs["recording_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InputIdentityConflict("Submission recording identity is invalid") from exc
    recording = session.get(Recording, recording_id)
    if (
        recording is None
        or recording.attempt_id != submission.attempt_id
        or recording.status != "SAVED"
    ):
        raise InputIdentityConflict("Submission recording identity is invalid")
    return {
        "recording_id": str(recording.recording_id),
        "sha256": recording.sha256,
        "media_type": recording.media_type,
        "storage_ref": recording.storage_ref,
    }


def _recording_identity(session: Session, submission: Submission) -> dict:
    recording = recording_input(session, submission)
    return {key: recording[key] for key in ("recording_id", "sha256", "media_type")}


def speaking_source(
    session: Session,
    submission: Submission,
    entry: PaperEntry,
    revision: TaskRevision,
    *,
    store: RecordingStore | None = None,
) -> dict:
    """Resolve the whole frozen audio source from immutable facts, never from an adapter."""
    from app.task_types.speaking import SpeakingSegment

    inputs = submission.inputs
    task = session.get(SessionTask, submission.session_task_id)
    attempt = session.get(Attempt, submission.attempt_id)
    persisted = session.get(Submission, submission.submission_id)
    if (
        not isinstance(inputs, dict)
        or inputs.get("schema") != "speaking-input-v1"
        or task is None
        or attempt is None
        or attempt.session_task_id != submission.session_task_id
        or persisted is None
        or persisted.attempt_id != submission.attempt_id
        or persisted.session_task_id != submission.session_task_id
        or persisted.inputs != inputs
        or task.paper_entry_id != entry.paper_entry_id
        or entry.task_revision_id != revision.task_revision_id
        or revision.task_type != "speaking"
        or inputs.get("paper_entry_id") != str(entry.paper_entry_id)
        or inputs.get("task_revision_id") != str(revision.task_revision_id)
        or not isinstance(inputs.get("segments"), list)
        or not inputs["segments"]
    ):
        raise InputIdentityConflict("Invalid Speaking source manifest")
    facts = list(
        session.scalars(
            select(SpeakingSegment)
            .where(SpeakingSegment.attempt_id == submission.attempt_id)
            .order_by(SpeakingSegment.position)
        )
    )
    if len(facts) != len(inputs["segments"]):
        raise InputIdentityConflict("Speaking source segment count mismatch")
    ordered = []
    for fact, frozen in zip(facts, inputs["segments"], strict=True):
        if not isinstance(frozen, dict):
            raise InputIdentityConflict("Invalid Speaking segment")
        recording = session.get(Recording, fact.recording_id)
        if (
            recording is None
            or recording.status != "SAVED"
            or recording.attempt_id != submission.attempt_id
            or frozen.get("segment_id") != str(fact.segment_id)
            or frozen.get("recording_id") != str(recording.recording_id)
            or type(frozen.get("position")) is not int
            or frozen["position"] != fact.position
            or frozen.get("storage_ref") != recording.storage_ref
            or frozen.get("sha256") != recording.sha256
            or frozen.get("media_type") != recording.media_type
        ):
            raise InputIdentityConflict("Speaking segment identity mismatch")
        if store is not None:
            try:
                store.verify(recording.storage_ref, recording.sha256)
            except (ValueError, OSError):
                raise InputIdentityConflict("Speaking recording unavailable") from None
        ordered.append(
            {
                "segment_id": str(fact.segment_id),
                "recording_id": str(recording.recording_id),
                "position": fact.position,
                "storage_ref": recording.storage_ref,
                "sha256": recording.sha256,
                "media_type": recording.media_type,
            }
        )
    source = {
        "schema": "speaking-source-v1",
        "submission_id": str(submission.submission_id),
        "attempt_id": str(submission.attempt_id),
        "paper_entry_id": str(entry.paper_entry_id),
        "task_revision_id": str(revision.task_revision_id),
        "submission_schema": inputs["schema"],
        "segments": ordered,
    }
    return {**source, "sha256": hashlib.sha256(_json(source).encode()).hexdigest()}


def valid_speaking_consumption(
    registry, requirement, source, dependencies, *, payload=None, derived_store=None
):
    """Validate common audio references, then the source-specific consumption contract."""
    if not isinstance(dependencies, dict):
        return False
    consumption = dependencies.get("speaking_consumption")
    if not isinstance(consumption, dict) or not consumption:
        return False
    ids = consumption.get("segment_ids")
    if ids is not None and (
        not isinstance(ids, list)
        or any(not isinstance(i, str) for i in ids)
        or len(ids) != len(set(ids))
        or not set(ids).issubset({s["segment_id"] for s in source["segments"]})
    ):
        return False
    derived = consumption.get("derived_recordings")
    if derived is not None:
        if not isinstance(derived, list):
            return False
        for item in derived:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and item[k]
                for k in ("storage_ref", "sha256", "media_type")
            ):
                return False
            if derived_store is not None:
                try:
                    derived_store.verify(item["storage_ref"], item["sha256"])
                except (ValueError, OSError):
                    return False
    frozen = _json(
        {
            "source": source,
            "dependencies": dependencies,
            "payload": payload,
            "requirement": requirement,
        }
    )
    valid = registry.validate_speaking_consumption(
        requirement, source, consumption, dependencies.get("asr"), payload
    )
    return valid and frozen == _json(
        {
            "source": source,
            "dependencies": dependencies,
            "payload": payload,
            "requirement": requirement,
        }
    )
