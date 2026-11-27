"""Explicit coordinator for formal, independently recoverable Speaking ASR calls."""

import hashlib
import re
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.identity import canonical_json as _json
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.source_identity import speaking_source
from app.evaluation.speaking_asr_contract import SCHEMA
from app.evaluation.speaking_asr_models import SpeakingFormalASRAttempt, SpeakingFormalASROperation
from app.evaluation.speaking_qwen_adapter import MAX_DATA_URI_BYTES, MAX_SECONDS, encoded_size
from app.evaluation.speaking_qwen_contract import (
    SOURCE,
    VERSION,
    valid_full_consumption,
    valid_parameters,
    validate_result,
)
from app.evaluation.worker import (
    KnownFailure,
    _lock_job_context,
    _skip,
)
from app.media.audio_inspection import AudioError
from app.media.audio_normalization import AudioSpec, prepare_audio
from app.papers.paper import EvaluationPlan, PaperEntry
from app.taskbank.models import TaskRevision


def prepare(engine, job_id, original_store, derived_store, tools):
    """Prepare actual audio outside a DB transaction and freeze it before any dispatch."""
    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return False
        job, submission, current = context
        if not current:
            if job.status != "READY":
                _skip(job)
            return False
        if job.status != "WAITING":
            return False
        if (job.requirement.get("source"), job.requirement.get("version")) != (
            SOURCE,
            VERSION,
        ) or not valid_parameters(job.requirement.get("parameters")):
            raise ValueError("Unsupported formal Speaking ASR contract")
        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        plan = session.get(EvaluationPlan, entry.paper_entry_id)
        if plan is None or plan.requirements[job.requirement_index] != job.requirement:
            raise InputIdentityConflict("Evaluation plan mismatch")
        source = speaking_source(session, submission, entry, revision)
        if job.input_refs is not None:
            if job.input_refs.get("speaking_source") != source:
                raise InputIdentityConflict("Speaking source changed")
            return True
        requirement = job.requirement
        submission_id = str(submission.submission_id)

    spec = AudioSpec(**requirement["parameters"]["audio_spec"])
    audio, derived = [], []
    for segment in source["segments"]:
        prepared = prepare_audio(segment, spec, original_store, derived_store, tools)
        if (
            prepared.original.duration_seconds > MAX_SECONDS
            or prepared.output.duration_seconds > MAX_SECONDS
            or encoded_size(prepared.output.byte_length) > MAX_DATA_URI_BYTES
        ):
            raise KnownFailure("ASR_AUDIO_INCOMPATIBLE")
        entry = {
            "segment_id": segment["segment_id"],
            "recording_id": segment["recording_id"],
            "position": segment["position"],
            "sha256": prepared.output.sha256,
            "storage_ref": prepared.storage_ref,
            "media_type": "audio/wav",
            "processing_version": spec.processing_version,
        }
        audio.append(entry)
        if not prepared.reused_original:
            derived.append({k: entry[k] for k in ("storage_ref", "sha256", "media_type")})
    consumption = {
        "segment_ids": [x["segment_id"] for x in audio],
        "audio_inputs": audio,
        "derived_recordings": derived,
    }
    payload = {
        "audio_inputs": audio,
        "submission_id": submission_id,
        "source_sha256": source["sha256"],
    }
    if not valid_full_consumption(source, consumption, requirement, None, payload):
        raise InputIdentityConflict("Invalid prepared audio identity")

    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return False
        job, submission, current = context
        if not current:
            if job.status != "READY":
                _skip(job)
            return False
        if job.status != "WAITING":
            return False
        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        if (
            job.requirement != requirement
            or speaking_source(session, submission, entry, revision) != source
        ):
            raise InputIdentityConflict("Speaking source changed during preparation")
        for item, original in zip(audio, source["segments"], strict=True):
            store = (
                original_store
                if item["sha256"] == original["sha256"]
                and item["storage_ref"] == original["storage_ref"]
                else derived_store
            )
            try:
                store.verify(item["storage_ref"], item["sha256"])
            except (ValueError, OSError):
                raise InputIdentityConflict("Prepared audio unavailable") from None
        refs = {
            "submission_id": submission_id,
            "task_revision_id": str(entry.task_revision_id),
            "paper_entry_id": str(entry.paper_entry_id),
            "dependencies": {"speaking_consumption": consumption},
            "speaking_source": source,
        }
        fingerprint = hashlib.sha256(
            _json({"refs": refs, "payload": payload, "requirement": requirement}).encode()
        ).hexdigest()
        if job.input_refs is not None:
            if job.input_refs != refs or job.input_fingerprint != fingerprint:
                raise InputIdentityConflict("Prepared audio changed")
            return True
        job.input_refs, job.input_fingerprint = refs, fingerprint
        for item in audio:
            session.add(
                SpeakingFormalASROperation(
                    job_id=job.job_id,
                    submission_id=job.submission_id,
                    segment_id=UUID(item["segment_id"]),
                    recording_id=UUID(item["recording_id"]),
                    position=item["position"],
                    source_sha256=source["sha256"],
                    audio_input=item,
                )
            )
        return True


def claim(engine, job_id, *, lease):
    if not isinstance(lease, timedelta) or lease <= timedelta(0):
        raise ValueError("Lease must be positive")
    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return None
        job, _, current = context
        if not current:
            if job.status != "READY":
                _skip(job)
            return None
        if job.status != "WAITING" or job.input_refs is None:
            return None
        operation = session.scalar(
            select(SpeakingFormalASROperation)
            .where(
                SpeakingFormalASROperation.job_id == job_id,
                SpeakingFormalASROperation.status == "WAITING",
            )
            .order_by(SpeakingFormalASROperation.position)
            .with_for_update(skip_locked=True)
        )
        if operation is None:
            return None
        now = session.scalar(select(func.clock_timestamp()))
        token = uuid4()
        operation.status, operation.claim_token, operation.lease_until = (
            "RUNNING",
            token,
            now + lease,
        )
        session.add(
            SpeakingFormalASRAttempt(
                execution_id=token, operation_id=operation.operation_id, status="RUNNING"
            )
        )
        return operation.operation_id, token


def execute(engine, operation_id, token, original_store, derived_store, provider):
    with Session(engine) as session:
        operation = session.get(SpeakingFormalASROperation, operation_id)
        if operation is None or operation.status != "RUNNING" or operation.claim_token != token:
            return False
        job_id, audio = operation.job_id, operation.audio_input
        job = session.get(EvaluationJob, job_id)
        original = next(
            (
                s
                for s in job.input_refs["speaking_source"]["segments"]
                if s["segment_id"] == audio["segment_id"]
            ),
            None,
        )
        if original is None:
            raise InputIdentityConflict("Segment not in source")
        store = (
            original_store
            if audio["storage_ref"] == original["storage_ref"]
            and audio["sha256"] == original["sha256"]
            else derived_store
        )
        params = job.requirement["parameters"]
    try:
        original_store.verify(original["storage_ref"], original["sha256"])
        data = store.read(audio["storage_ref"], audio["sha256"])
    except (ValueError, OSError):
        return _finish_operation(
            engine, operation_id, token, "FAILED", error="AUDIO_UNAVAILABLE", before_dispatch=True
        )
    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return False
        job, _, current = context
        op = session.scalar(
            select(SpeakingFormalASROperation)
            .where(SpeakingFormalASROperation.operation_id == operation_id)
            .with_for_update()
        )
        attempt = session.get(SpeakingFormalASRAttempt, token)
        now = session.scalar(select(func.clock_timestamp()))
        if (
            op is None
            or op.status != "RUNNING"
            or op.claim_token != token
            or op.lease_until <= now
            or attempt.dispatch_started_at is not None
        ):
            return False
        if not current:
            op.status, op.claim_token, op.lease_until = "WAITING", None, None
            attempt.status, attempt.finished_at = "UNDISPATCHED", now
            _skip(job)
            return False
        attempt.dispatch_started_at = now
    try:
        result = provider.recognize(data, params)
        if (
            not isinstance(result, dict)
            or result.get("status") != "TEXT"
            or not isinstance(result.get("text"), str)
            or not result["text"].strip()
        ):
            return _finish_operation(
                engine, operation_id, token, "INVALID", error="ASR_INVALID_RESPONSE"
            )
        value = result.get("request_id")
        result = {
            "status": "TEXT",
            "text": result["text"],
            "request_id": value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
            else None,
        }
    except KnownFailure as exc:
        status = (
            "INVALID"
            if exc.code in {"ASR_INVALID_RESPONSE", "ASR_UNCONFIRMED_RESULT"}
            else "FAILED"
        )
        return _finish_operation(
            engine,
            operation_id,
            token,
            status,
            error=exc.code,
            request_id=getattr(exc, "request_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        return _finish_operation(
            engine,
            operation_id,
            token,
            "UNKNOWN",
            error="EXTERNAL_OUTCOME_UNKNOWN",
            request_id=getattr(exc, "request_id", None),
        )
    return _finish_operation(engine, operation_id, token, "READY", result=result)


def _finish_operation(
    engine,
    operation_id,
    token,
    status,
    *,
    result=None,
    error=None,
    before_dispatch=False,
    request_id=None,
):
    with Session(engine) as session, session.begin():
        identity = session.get(SpeakingFormalASROperation, operation_id)
        if identity is None:
            return False
        context = _lock_job_context(session, identity.job_id)
        if context is None:
            return False
        job, _, current = context
        op = session.scalar(
            select(SpeakingFormalASROperation)
            .where(SpeakingFormalASROperation.operation_id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempt = session.get(SpeakingFormalASRAttempt, token)
        now = session.scalar(select(func.clock_timestamp()))
        if (
            op.status != "RUNNING"
            or op.claim_token != token
            or op.lease_until <= now
            or attempt.status != "RUNNING"
            or (before_dispatch and attempt.dispatch_started_at is not None)
        ):
            return False
        op.status, op.result, op.error_code = status, result, error
        op.claim_token, op.lease_until = None, None
        attempt.status, attempt.error_code, attempt.finished_at = status, error, now
        if result is not None:
            value = result.get("request_id")
            attempt.request_id = (
                value
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)
                else None
            )
        elif isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            attempt.request_id = request_id
        if not current:
            _skip(job)
        elif status != "READY" and (job.status != "UNKNOWN" or status == "UNKNOWN"):
            job.status, job.error_code = status, error
        return True


def recover_expired(session, *, job_id=None):
    query = (
        select(SpeakingFormalASROperation.operation_id, SpeakingFormalASROperation.job_id)
        .where(
            SpeakingFormalASROperation.status == "RUNNING",
            SpeakingFormalASROperation.lease_until <= func.clock_timestamp(),
        )
        .order_by(SpeakingFormalASROperation.job_id, SpeakingFormalASROperation.operation_id)
    )
    if job_id is not None:
        query = query.where(SpeakingFormalASROperation.job_id == job_id)
    identities = session.execute(query).all()
    count = 0
    for operation_id, owner_job_id in identities:
        context = _lock_job_context(session, owner_job_id)
        if context is None:
            continue
        job, _, current = context
        op = session.scalar(
            select(SpeakingFormalASROperation)
            .where(
                SpeakingFormalASROperation.operation_id == operation_id,
                SpeakingFormalASROperation.status == "RUNNING",
                SpeakingFormalASROperation.lease_until <= func.clock_timestamp(),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if op is None:
            continue
        count += 1
        attempt = session.get(SpeakingFormalASRAttempt, op.claim_token)
        now = session.scalar(select(func.clock_timestamp()))
        status = "UNKNOWN" if attempt.dispatch_started_at else "WAITING"
        op.status, op.error_code, op.claim_token, op.lease_until = (
            status,
            "LEASE_EXPIRED_OUTCOME_UNKNOWN" if status == "UNKNOWN" else None,
            None,
            None,
        )
        attempt.status, attempt.error_code, attempt.finished_at = (
            ("UNKNOWN" if status == "UNKNOWN" else "UNDISPATCHED"),
            op.error_code,
            now,
        )
        if not current:
            if job.status != "READY":
                _skip(job)
        elif status == "UNKNOWN" and job.status in ("WAITING", "FAILED", "INVALID"):
            job.status, job.error_code = "UNKNOWN", op.error_code
    return count


def authorize_retry(session, operation_id, *, accept_unknown=False):
    identity = session.get(SpeakingFormalASROperation, operation_id)
    if identity is None:
        raise ValueError("Unknown operation")
    context = _lock_job_context(session, identity.job_id)
    if context is None or not context[2]:
        raise ValueError("Attempt is no longer current")
    job = context[0]
    op = session.scalar(
        select(SpeakingFormalASROperation)
        .where(SpeakingFormalASROperation.operation_id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if op.status not in ("FAILED", "INVALID", "UNKNOWN") or (
        op.status == "UNKNOWN" and not accept_unknown
    ):
        raise ValueError("Operation cannot be retried without authorization")
    op.status, op.error_code = "WAITING", None
    if job.status in ("FAILED", "INVALID", "UNKNOWN"):
        job.status, job.error_code = "WAITING", None


def aggregate(engine, job_id):
    with Session(engine) as session, session.begin():
        context = _lock_job_context(session, job_id)
        if context is None:
            return False
        job, submission, current = context
        if not current:
            if job.status != "READY":
                _skip(job)
            return False
        if job.status == "READY":
            return True
        if job.status != "WAITING" or not job.input_refs:
            return False
        source = job.input_refs["speaking_source"]
        consumption = job.input_refs["dependencies"]["speaking_consumption"]
        entry = session.get(PaperEntry, job.paper_entry_id)
        revision = session.get(TaskRevision, entry.task_revision_id)
        if speaking_source(session, submission, entry, revision) != source:
            raise InputIdentityConflict("Speaking source changed")
        snapshot = {
            "audio_inputs": consumption["audio_inputs"],
            "submission_id": str(submission.submission_id),
            "source_sha256": source["sha256"],
        }
        digest = hashlib.sha256(
            _json(
                {"refs": job.input_refs, "payload": snapshot, "requirement": job.requirement}
            ).encode()
        ).hexdigest()
        if digest != job.input_fingerprint:
            raise InputIdentityConflict("Speaking input fingerprint mismatch")
        operations = list(
            session.scalars(
                select(SpeakingFormalASROperation)
                .where(SpeakingFormalASROperation.job_id == job_id)
                .order_by(SpeakingFormalASROperation.position)
                .with_for_update()
            )
        )
        if len(operations) != len(consumption["audio_inputs"]) or any(
            op.status != "READY" for op in operations
        ):
            return False
        segments = []
        for op, audio in zip(operations, consumption["audio_inputs"], strict=True):
            if (
                op.audio_input != audio
                or op.source_sha256 != source["sha256"]
                or op.submission_id != submission.submission_id
            ):
                raise InputIdentityConflict("ASR operation identity mismatch")
            segments.append(
                {
                    "segment_id": audio["segment_id"],
                    "recording_id": audio["recording_id"],
                    "position": audio["position"],
                    "input_sha256": audio["sha256"],
                    "processing_version": audio["processing_version"],
                    "status": op.result["status"],
                    "text": op.result["text"],
                }
            )
        payload = {
            "schema": SCHEMA,
            "submission_id": str(submission.submission_id),
            "speaking_source_sha256": source["sha256"],
            "contract": {"source": SOURCE, "version": VERSION},
            "segments": segments,
        }
        validate_result(
            payload,
            job.requirement,
            source,
            consumption,
            "dashscope",
            job.requirement["parameters"]["model"],
        )
        session.add(
            RawEvaluation(
                evaluation_id=uuid4(),
                job_id=job_id,
                submission_id=submission.submission_id,
                execution_id=uuid4(),
                status="READY",
                input_refs=job.input_refs,
                input_fingerprint=job.input_fingerprint,
                provider="dashscope",
                model=job.requirement["parameters"]["model"],
                payload=payload,
            )
        )
        job.status, job.error_code = "READY", None
        return True


def run_job(engine, job_id, *, original_store, derived_store, tools, provider, lease):
    """Execute only this explicit job; failed/unknown segments are never auto-retried."""
    with Session(engine) as session, session.begin():
        recover_expired(session, job_id=job_id)
    try:
        if not prepare(engine, job_id, original_store, derived_store, tools):
            return False
    except (KnownFailure, AudioError) as exc:
        with Session(engine) as session, session.begin():
            context = _lock_job_context(session, job_id)
            if context is not None and context[2] and context[0].status == "WAITING":
                context[0].error_code = exc.code
        return False
    while True:
        work = claim(engine, job_id, lease=lease)
        if work is None:
            break
        if not execute(engine, *work, original_store, derived_store, provider):
            break
    return aggregate(engine, job_id)
