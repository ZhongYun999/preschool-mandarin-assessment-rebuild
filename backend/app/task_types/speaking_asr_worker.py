"""Explicit worker for durable Speaking process recognition jobs.

External calls occur only after an explicit worker invocation and outside DB transactions.
"""

import argparse
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.evaluation.asr_contract import MODEL, SOURCE, VERSION, valid_parameters
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.worker import AdapterResult, InputSnapshot, KnownFailure
from app.media.audio_inspection import AudioTools
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.task_types.speaking import SpeakingSegment
from app.task_types.speaking_asr import SpeakingASR


@dataclass(frozen=True)
class Claim:
    operation_id: UUID
    token: UUID
    requirement: dict
    recording: dict


def claim(engine, operation_id, requirement, lease=timedelta(minutes=5)):
    if lease <= timedelta(0):
        raise ValueError("Lease must be positive")
    if (
        not isinstance(requirement, dict)
        or not isinstance(requirement.get("source"), str)
        or not isinstance(requirement.get("version"), str)
        or not isinstance(requirement.get("parameters"), dict)
        or not isinstance(requirement["parameters"].get("region"), str)
        or not isinstance(requirement["parameters"].get("workspace"), str)
    ):
        raise TypeError("Invalid recognition contract")
    with Session(engine) as s, s.begin():
        identity = s.execute(
            select(
                SpeakingASR.attempt_id,
                SpeakingSegment.attempt_id,
                Attempt.session_task_id,
                SessionTask.session_id,
            )
            .join(SpeakingSegment, SpeakingSegment.segment_id == SpeakingASR.segment_id)
            .join(Attempt, Attempt.attempt_id == SpeakingASR.attempt_id)
            .join(SessionTask, SessionTask.session_task_id == Attempt.session_task_id)
            .where(SpeakingASR.operation_id == operation_id)
        ).one_or_none()
        if identity is None or identity[0] != identity[1]:
            return None
        _, _, task_id, session_id = identity
        owner = s.scalar(
            select(AssessmentSession)
            .where(AssessmentSession.session_id == session_id)
            .with_for_update()
        )
        task = s.scalar(
            select(SessionTask).where(SessionTask.session_task_id == task_id).with_for_update()
        )
        attempt = s.scalar(
            select(Attempt).where(Attempt.attempt_id == identity[0]).with_for_update()
        )
        job = s.scalar(
            select(SpeakingASR).where(SpeakingASR.operation_id == operation_id).with_for_update()
        )
        if (
            owner.status != "IN_PROGRESS"
            or task.current_attempt_id != attempt.attempt_id
            or attempt.status != "OPEN"
            or job.status != "WAITING"
        ):
            return None
        record = s.get(Recording, job.recording_id)
        if (
            record is None
            or record.status != "SAVED"
            or record.attempt_id != job.attempt_id
            or record.sha256 != job.sha256
        ):
            job.status, job.error_code = "FAILED", "INPUT_IDENTITY_MISMATCH"
            return None
        if job.contract is not None and job.contract != requirement:
            raise ValueError("Recognition contract differs from frozen contract")
        now = s.scalar(select(func.clock_timestamp()))
        token = uuid4()
        job.contract, job.status, job.claim_token, job.lease_until = (
            requirement,
            "RUNNING",
            token,
            now + lease,
        )
        job.dispatched_at, job.error_code, job.attempts = None, None, job.attempts + 1
        return Claim(
            job.operation_id,
            token,
            requirement,
            {
                "recording_id": str(record.recording_id),
                "sha256": record.sha256,
                "media_type": record.media_type,
                "storage_ref": record.storage_ref,
            },
        )


def execute(engine, work, adapter):
    with Session(engine) as s, s.begin():
        identity = s.execute(
            select(SpeakingASR.attempt_id, Attempt.session_task_id, SessionTask.session_id)
            .join(Attempt, Attempt.attempt_id == SpeakingASR.attempt_id)
            .join(SessionTask, SessionTask.session_task_id == Attempt.session_task_id)
            .where(SpeakingASR.operation_id == work.operation_id)
        ).one_or_none()
        if identity is None:
            return False
        owner = s.scalar(
            select(AssessmentSession)
            .where(AssessmentSession.session_id == identity.session_id)
            .with_for_update()
        )
        task = s.scalar(
            select(SessionTask)
            .where(SessionTask.session_task_id == identity.session_task_id)
            .with_for_update()
        )
        attempt = s.scalar(
            select(Attempt).where(Attempt.attempt_id == identity.attempt_id).with_for_update()
        )
        job = s.scalar(
            select(SpeakingASR)
            .where(SpeakingASR.operation_id == work.operation_id)
            .with_for_update()
        )
        now = s.scalar(select(func.clock_timestamp()))
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != work.token
            or job.lease_until <= now
            or job.dispatched_at is not None
        ):
            return False
        if (
            owner.status != "IN_PROGRESS"
            or task.current_attempt_id != attempt.attempt_id
            or attempt.status != "OPEN"
        ):
            job.status, job.claim_token, job.lease_until = "WAITING", None, None
            return False
        record = s.get(Recording, job.recording_id)
        if (
            record is None
            or job.contract != work.requirement
            or work.recording
            != {
                "recording_id": str(record.recording_id),
                "sha256": record.sha256,
                "media_type": record.media_type,
                "storage_ref": record.storage_ref,
            }
            or record.status != "SAVED"
            or record.sha256 != job.sha256
            or record.attempt_id != job.attempt_id
        ):
            job.status, job.error_code = "FAILED", "INPUT_IDENTITY_MISMATCH"
            job.claim_token, job.lease_until = None, None
            return False
        # A conservative dispatch boundary: after this point a crash may mean a charged call.
        job.dispatched_at = now
    p = work.requirement["parameters"]
    refs = {"endpoint": f"https://{p['workspace']}.{p['region']}.maas.aliyuncs.com/api/v1"}
    inputs = InputSnapshot(refs=refs, payload={"recording": work.recording})
    try:
        result = adapter.evaluate(inputs, work.requirement)
        if not isinstance(result, AdapterResult) or not adapter.validate_result(
            result, work.requirement
        ):
            raise KnownFailure("ASR_INVALID_RESPONSE")
        kind, value = result.payload.get("status"), result.payload.get("text")
        if kind == "TEXT" and isinstance(value, str) and value.strip():
            semantic = "TRANSCRIBED"
        elif (
            kind == "NO_TEXT"
            and value is None
            and getattr(adapter, "confirmed_no_speech", False) is True
        ):
            # Only an explicitly confirmed adapter may attest no-speech semantics.
            semantic = "NO_RECOGNIZED_SPEECH"
        else:
            raise KnownFailure("ASR_UNCONFIRMED_RESULT")
        status, error = "READY", None
    except KnownFailure as exc:
        result, semantic, value, status, error = None, None, None, "FAILED", exc.code
    except Exception:  # noqa: BLE001 - external outcome may be unknown
        result, semantic, value, status, error = (
            None,
            None,
            None,
            "UNKNOWN",
            "EXTERNAL_OUTCOME_UNKNOWN",
        )
    with Session(engine) as s, s.begin():
        job = s.scalar(
            select(SpeakingASR)
            .where(SpeakingASR.operation_id == work.operation_id)
            .with_for_update()
        )
        now = s.scalar(select(func.clock_timestamp()))
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != work.token
            or job.lease_until <= now
        ):
            return False
        job.status, job.error_code, job.semantic, job.transcript = status, error, semantic, value
        if result is not None:
            job.provider, job.model = result.provider, result.model
            request_id = result.payload.get("request_id")
            job.request_id = request_id if isinstance(request_id, str) else None
        job.claim_token, job.lease_until = None, None
        return True


def recover_expired(session):
    now = session.scalar(select(func.clock_timestamp()))
    jobs = session.scalars(
        select(SpeakingASR)
        .where(SpeakingASR.status == "RUNNING", SpeakingASR.lease_until <= now)
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        job.status = "UNKNOWN" if job.dispatched_at else "WAITING"
        job.error_code = "LEASE_EXPIRED_OUTCOME_UNKNOWN" if job.dispatched_at else None
        job.claim_token, job.lease_until = None, None
    return len(jobs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation_id", type=UUID)
    parser.add_argument("--region", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    tools = AudioTools()
    parameters = {
        "model": MODEL,
        "region": args.region,
        "workspace": args.workspace,
        "enable_itn": True,
        "language": "zh",
        "audio_spec": {
            "container": "wav",
            "codec": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "processing_version": "1",
            "ffmpeg_version": tools.version(),
            "ffprobe_version": tools.version(probe=True),
        },
    }
    if not valid_parameters(parameters):
        raise ValueError("Invalid provider configuration")
    requirement = {"source": SOURCE, "version": VERSION, "parameters": parameters}
    engine = create_database_engine()
    try:
        with Session(engine) as s, s.begin():
            recover_expired(s)
        work = claim(engine, args.operation_id, requirement)
        if work is None:
            raise ValueError("Operation is not available")
        execute(engine, work, DashScopeASR(RecordingStore(), RecordingStore(), tools))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
