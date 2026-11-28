"""Durable scheduling and short-transaction worker operations.

Adapters are trusted deployed code: resolve_inputs only reads persisted facts and
performs no external I/O. None means an upstream dependency is not ready. The
adapter must validate the versioned output contract, and execute outside the DB
transaction. Adapters are supplied explicitly by the worker caller.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.identity import canonical_json as _json
from app.evaluation.models import EvaluationDiagnosticEvidence, EvaluationJob, RawEvaluation
from app.evaluation.source_identity import (
    _recording_identity,
    speaking_source,
    valid_speaking_consumption,
)
from app.evaluation.upstream_evidence import upstream_asr
from app.media.recording_store import RecordingStore
from app.papers.paper import EvaluationPlan, PaperEntry
from app.scoring.registry import Registry
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.sessions.close_manifest import CloseManifest, CloseManifestEntry
from app.sessions.submission import EvaluationIntent, Submission
from app.taskbank.models import TaskRevision


@dataclass(frozen=True)
class InputSnapshot:
    refs: dict
    payload: dict


@dataclass(frozen=True)
class AdapterResult:
    payload: dict
    provider: str | None = None
    model: str | None = None
    engine_version: str | None = None


class Adapter(Protocol):
    def resolve_inputs(
        self, session: Session, submission: Submission, revision: TaskRevision, requirement: dict
    ) -> InputSnapshot | None: ...
    def evaluate(self, inputs: InputSnapshot, requirement: dict) -> AdapterResult: ...
    def validate_result(self, result: AdapterResult, requirement: dict) -> bool: ...


class KnownFailure(Exception):
    """A classified failure whose external outcome is known."""

    def __init__(self, code: str, *, diagnostic: dict | None = None):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
            raise ValueError("Invalid diagnostic code")
        self.code = code
        if diagnostic is not None and not isinstance(diagnostic, dict):
            raise ValueError("Invalid diagnostic evidence")
        self.diagnostic = diagnostic
        super().__init__(code)


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    token: UUID
    inputs: InputSnapshot
    requirement: dict
    adapter: Adapter
    registry: Registry
    recording_store: RecordingStore | None = None
    derived_recording_store: RecordingStore | None = None


def schedule(
    session: Session, *, limit: int | None = None, submission_id: UUID | None = None
) -> int:
    """Create at most one job per genuine, frozen intent in the caller transaction."""
    query = (
        select(EvaluationIntent, Submission, SessionTask, EvaluationPlan)
        .join(Submission, Submission.submission_id == EvaluationIntent.submission_id)
        .join(SessionTask, SessionTask.session_task_id == Submission.session_task_id)
        .join(EvaluationPlan, EvaluationPlan.paper_entry_id == EvaluationIntent.paper_entry_id)
        .where(EvaluationIntent.scheduled_at.is_(None))
        .order_by(EvaluationIntent.intent_id)
        .limit(limit)
    )
    if submission_id is not None:
        query = query.where(Submission.submission_id == submission_id)
    rows = session.execute(query).all()
    count = 0
    for intent, submission, task, plan in rows:
        if (
            task.paper_entry_id != intent.paper_entry_id
            or intent.requirement_index < 0
            or intent.requirement_index >= len(plan.requirements)
        ):
            raise ValueError("Evaluation intent does not match its frozen plan")
        requirement = plan.requirements[intent.requirement_index]
        if (
            not isinstance(requirement, dict)
            or set(requirement)
            not in (
                {"source", "version", "parameters"},
                {"source", "version", "parameters", "depends_on"},
            )
            or not isinstance(requirement["source"], str)
            or not isinstance(requirement["version"], str)
            or not isinstance(requirement["parameters"], dict)
        ):
            raise ValueError("Invalid frozen evaluation requirement")
        created = session.scalar(
            insert(EvaluationJob)
            .values(
                job_id=uuid4(),
                intent_id=intent.intent_id,
                submission_id=submission.submission_id,
                paper_entry_id=intent.paper_entry_id,
                requirement_index=intent.requirement_index,
                requirement=requirement,
                status="WAITING",
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["intent_id"])
            .returning(EvaluationJob.job_id)
        )
        count += created is not None
        session.execute(
            update(EvaluationIntent)
            .where(
                EvaluationIntent.intent_id == intent.intent_id,
                EvaluationIntent.scheduled_at.is_(None),
            )
            .values(scheduled_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )
        # The conditional update can lose to another dispatcher. Refresh on
        # subsequent ORM reads instead of exposing a stale pending marker.
        session.expire(intent, ["scheduled_at"])
    return count


def _lock_job_context(session: Session, job_id: UUID):
    """Use the Session → SessionTask → Attempt → Job order used by redo and close."""
    identity = session.execute(
        select(
            Submission.submission_id,
            Submission.session_task_id,
            Submission.attempt_id,
            SessionTask.session_id,
        )
        .join(EvaluationJob, EvaluationJob.submission_id == Submission.submission_id)
        .join(SessionTask, SessionTask.session_task_id == Submission.session_task_id)
        .where(EvaluationJob.job_id == job_id)
    ).one_or_none()
    if identity is None:
        return None
    submission_id, task_id, attempt_id, session_id = identity
    owner = session.scalar(
        select(AssessmentSession)
        .where(AssessmentSession.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    task = session.scalar(
        select(SessionTask)
        .where(SessionTask.session_task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = session.scalar(
        select(Attempt)
        .where(Attempt.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = session.scalar(
        select(EvaluationJob)
        .where(EvaluationJob.job_id == job_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if job is None:
        return None
    if owner.status == "IN_PROGRESS":
        current = task.current_attempt_id == attempt_id and attempt.status == "SEALED"
    elif owner.status in ("INPUT_CLOSED", "COMPLETED"):
        current = (
            session.scalar(
                select(CloseManifestEntry.submission_id)
                .join(CloseManifest, CloseManifest.manifest_id == CloseManifestEntry.manifest_id)
                .where(
                    CloseManifest.session_id == session_id,
                    CloseManifestEntry.session_task_id == task_id,
                    CloseManifestEntry.submission_id == submission_id,
                )
            )
            is not None
        )
    else:
        current = False
    return job, session.get(Submission, submission_id), current


def _skip(job: EvaluationJob) -> None:
    job.status, job.error_code = "SKIPPED", "SUPERSEDED"
    job.claim_token, job.lease_until = None, None


def _claim_job(
    engine: Engine,
    job_id: UUID,
    *,
    registry: Registry,
    adapters: Mapping[tuple[str, str], Adapter],
    lease: timedelta,
    recording_store: RecordingStore | None = None,
    derived_recording_store: RecordingStore | None = None,
) -> Claim | None:
    if not isinstance(lease, timedelta) or lease <= timedelta(0):
        raise ValueError("Lease must be positive")
    with Session(engine) as s, s.begin():
        context = _lock_job_context(s, job_id)
        if context is None:
            return None
        job, submission, current = context
        if job.status != "WAITING":
            return None
        if not current:
            _skip(job)
            s.flush()
            return None
        job.last_checked_at = s.scalar(select(func.clock_timestamp()))
        if (job.requirement.get("source"), job.requirement.get("version")) == (
            "dashscope-speaking-qwen3-asr",
            "1",
        ):
            job.error_code = "EVALUATION_REQUIRES_SEGMENT_COORDINATOR"
            return None
        entry = s.get(PaperEntry, job.paper_entry_id)
        revision = s.get(TaskRevision, entry.task_revision_id)
        plan = s.get(EvaluationPlan, job.paper_entry_id)
        if (
            plan is None
            or plan.task_revision_id != revision.task_revision_id
            or job.requirement_index < 0
            or job.requirement_index >= len(plan.requirements)
            or plan.requirements[job.requirement_index] != job.requirement
        ):
            raise ValueError("Frozen evaluation dependency plan is invalid")
        adapter = adapters.get((job.requirement["source"], job.requirement["version"]))
        if adapter is None:
            job.error_code = "EVALUATION_ADAPTER_UNAVAILABLE"
            return None
        if not registry.supports(revision.task_type, job.requirement):
            job.error_code = "EVALUATION_CONTRACT_UNAVAILABLE"
            return None
        if not registry.valid_dependency(
            revision.task_type, plan.requirements, job.requirement_index
        ):
            return None
        source = (
            speaking_source(
                s, submission, entry, revision, store=recording_store or RecordingStore()
            )
            if revision.task_type == "speaking"
            else None
        )
        upstream = upstream_asr(s, job, submission, revision, plan, registry, source)
        if "depends_on" in job.requirement and upstream is None:
            return None
        inputs = adapter.resolve_inputs(s, submission, revision, job.requirement)
        if inputs is None:
            return None
        if (
            not isinstance(inputs, InputSnapshot)
            or not isinstance(inputs.refs, dict)
            or not isinstance(inputs.payload, dict)
        ):
            raise TypeError("Adapter inputs must be structured facts")
        inputs = InputSnapshot(json.loads(_json(inputs.refs)), json.loads(_json(inputs.payload)))
        if registry.recognition_contract(job.requirement) and source is None:
            recording = _recording_identity(s, submission)
            if any(inputs.refs.get(k) != v for k, v in recording.items()):
                raise InputIdentityConflict("Recognition input recording identity mismatch")
        if upstream is not None:
            if "asr" in inputs.refs or "asr" in inputs.payload:
                raise ValueError("Reserved upstream input key")
            inputs.refs["asr"], inputs.payload["asr"] = upstream
        if source is not None and not valid_speaking_consumption(
            registry,
            job.requirement,
            source,
            inputs.refs,
            payload=inputs.payload,
            derived_store=derived_recording_store or recording_store or RecordingStore(),
        ):
            raise InputIdentityConflict("Speaking consumption identity is invalid")
        refs = {
            "submission_id": str(submission.submission_id),
            "task_revision_id": str(revision.task_revision_id),
            "paper_entry_id": str(entry.paper_entry_id),
            "dependencies": inputs.refs,
        }
        if source is not None:
            refs["speaking_source"] = source
        digest = hashlib.sha256(
            _json(
                {"refs": refs, "payload": inputs.payload, "requirement": job.requirement}
            ).encode()
        ).hexdigest()
        if job.input_fingerprint is not None and (
            job.input_fingerprint != digest or job.input_refs != refs
        ):
            raise InputIdentityConflict("Evaluation inputs changed after first claim")
        now = s.scalar(select(func.clock_timestamp()))
        prepare = getattr(adapter, "prepare_execution", None)
        if prepare is not None:
            from app.evaluation.provider_config import CredentialUnavailable

            try:
                job.execution_snapshot, adapter = prepare(s, now)
            except CredentialUnavailable:
                job.error_code = "LLM_PROVIDER_UNAVAILABLE"
                s.flush()
                return None
        else:
            job.execution_snapshot = None
        token = uuid4()
        job.input_refs, job.input_fingerprint = refs, digest
        job.claim_token, job.lease_until = token, now + lease
        job.dispatch_started_at = None
        job.status, job.error_code = "RUNNING", None
        job.attempts += 1
        s.flush()
        return Claim(
            job.job_id,
            token,
            inputs,
            json.loads(_json(job.requirement)),
            adapter,
            registry,
            recording_store or RecordingStore() if source is not None else None,
            (derived_recording_store or recording_store or RecordingStore())
            if source is not None
            else None,
        )


def claim_job(
    engine: Engine,
    job_id: UUID,
    *,
    registry: Registry,
    adapters: Mapping[tuple[str, str], Adapter],
    lease: timedelta,
    recording_store: RecordingStore | None = None,
    derived_recording_store: RecordingStore | None = None,
) -> Claim | None:
    try:
        return _claim_job(
            engine,
            job_id,
            registry=registry,
            adapters=adapters,
            lease=lease,
            recording_store=recording_store,
            derived_recording_store=derived_recording_store,
        )
    except InputIdentityConflict:
        # The failed claim rolled back. Preserve a non-sensitive diagnostic without
        # changing the frozen input, using the same session-to-job lock order.
        with Session(engine) as session, session.begin():
            context = _lock_job_context(session, job_id)
            if context is not None:
                job, _, current = context
                if current and job.status == "WAITING":
                    job.error_code = "INPUT_IDENTITY_CONFLICT"
                    job.last_checked_at = session.scalar(select(func.clock_timestamp()))
                    session.flush()
        raise


def _finish(
    engine: Engine,
    claim: Claim,
    *,
    status: str,
    result: AdapterResult | None = None,
    error_code: str | None = None,
    diagnostic: dict | None = None,
) -> bool:
    with Session(engine) as s, s.begin():
        job = s.scalar(
            select(EvaluationJob)
            .where(EvaluationJob.job_id == claim.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = s.scalar(select(func.clock_timestamp()))
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != claim.token
            or job.lease_until <= now
        ):
            return False
        raw = RawEvaluation(
            evaluation_id=uuid4(),
            job_id=job.job_id,
            submission_id=job.submission_id,
            execution_id=claim.token,
            status=status,
            input_refs=job.input_refs,
            input_fingerprint=job.input_fingerprint,
            execution_snapshot=job.execution_snapshot,
            provider=result.provider if result else None,
            model=result.model if result else None,
            engine_version=result.engine_version if result else None,
            payload=result.payload if status == "READY" else None,
            error_code=error_code,
        )
        s.add(raw)
        if diagnostic is not None:
            if status == "READY":
                raise ValueError("Diagnostics require an unsuccessful evaluation")
            s.flush()
            s.add(
                EvaluationDiagnosticEvidence(evaluation_id=raw.evaluation_id, evidence=diagnostic)
            )
        job.status, job.error_code = status, error_code
        job.claim_token, job.lease_until = None, None
        s.flush()
        return True


def execute_claim(engine: Engine, claim: Claim) -> bool:
    """Mark dispatch under the session lock, then call the adapter without a DB transaction."""
    recognition_source = None
    with Session(engine) as s, s.begin():
        context = _lock_job_context(s, claim.job_id)
        if context is None:
            return False
        live, submission, current = context
        now = s.scalar(select(func.clock_timestamp()))
        if (
            live.status != "RUNNING"
            or live.claim_token != claim.token
            or live.lease_until <= now
            or live.dispatch_started_at is not None
        ):
            return False
        if not current:
            _skip(live)
            s.flush()
            return False
        entry = s.get(PaperEntry, live.paper_entry_id)
        revision = s.get(TaskRevision, entry.task_revision_id)
        if revision.task_type == "speaking":
            try:
                source = speaking_source(
                    s, submission, entry, revision, store=claim.recording_store or RecordingStore()
                )
                if source != live.input_refs.get(
                    "speaking_source"
                ) or not valid_speaking_consumption(
                    claim.registry,
                    live.requirement,
                    source,
                    claim.inputs.refs,
                    payload=claim.inputs.payload,
                    derived_store=claim.derived_recording_store or RecordingStore(),
                ):
                    raise InputIdentityConflict("Speaking input changed before dispatch")
                recognition_source = source
            except InputIdentityConflict:
                live.status, live.claim_token, live.lease_until = "WAITING", None, None
                live.error_code = "INPUT_IDENTITY_CONFLICT"
                return False
        fingerprint = hashlib.sha256(
            _json(
                {
                    "refs": live.input_refs,
                    "payload": claim.inputs.payload,
                    "requirement": claim.requirement,
                }
            ).encode()
        ).hexdigest()
        if (
            live.input_refs.get("dependencies") != claim.inputs.refs
            or live.requirement != claim.requirement
            or live.input_fingerprint != fingerprint
        ):
            raise ValueError("Claim does not match frozen input identity")
        adapter_snapshot = getattr(claim.adapter, "snapshot", None)
        if live.execution_snapshot != adapter_snapshot:
            raise ValueError("Claim does not match execution snapshot")
        # The committed marker is the dispatch boundary. Redo after it may race
        # an external request; a crash is conservatively treated as UNKNOWN.
        live.dispatch_started_at = now
        s.flush()
    inputs = InputSnapshot(
        json.loads(_json(claim.inputs.refs)), json.loads(_json(claim.inputs.payload))
    )
    requirement = json.loads(_json(claim.requirement))
    try:
        result = claim.adapter.evaluate(inputs, requirement)
    except KnownFailure as exc:
        return _finish(
            engine, claim, status="FAILED", error_code=exc.code, diagnostic=exc.diagnostic
        )
    except Exception:  # noqa: BLE001 - external outcome may be unknown; do not log private payloads
        return _finish(engine, claim, status="UNKNOWN", error_code="EXTERNAL_OUTCOME_UNKNOWN")
    try:
        if not isinstance(result, AdapterResult) or not isinstance(result.payload, dict):
            raise TypeError("Invalid result")
        _json(result.payload)
        if (
            _json(inputs.refs) != _json(claim.inputs.refs)
            or _json(inputs.payload) != _json(claim.inputs.payload)
            or _json(requirement) != _json(claim.requirement)
        ):
            raise ValueError("Adapter changed frozen inputs")
        if any(
            value is not None and not isinstance(value, str)
            for value in (result.provider, result.model, result.engine_version)
        ):
            raise TypeError("Invalid engine identity")
        if not claim.adapter.validate_result(result, requirement):
            raise ValueError("Invalid contract")
        if claim.registry.recognition_contract(requirement):
            claim.registry.recognition_result(
                requirement,
                result.payload,
                speaking_source=recognition_source,
                speaking_consumption=claim.inputs.refs.get("speaking_consumption"),
                provider=result.provider,
                model=result.model,
            )
    except Exception:  # noqa: BLE001 - invalid contract output is not a score
        metadata = None
        if isinstance(result, AdapterResult):
            metadata = AdapterResult(
                payload={},
                provider=result.provider if isinstance(result.provider, str) else None,
                model=result.model if isinstance(result.model, str) else None,
                engine_version=result.engine_version
                if isinstance(result.engine_version, str)
                else None,
            )
        return _finish(
            engine, claim, status="INVALID", result=metadata, error_code="INVALID_RESULT"
        )
    return _finish(engine, claim, status="READY", result=result)


def recover_expired(session: Session, *, limit: int | None = None) -> int:
    """Requeue undispatched leases; preserve ambiguous external outcomes as UNKNOWN."""
    jobs = session.scalars(
        select(EvaluationJob)
        .where(
            EvaluationJob.status == "RUNNING", EvaluationJob.lease_until <= func.clock_timestamp()
        )
        .order_by(EvaluationJob.lease_until, EvaluationJob.job_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for job in jobs:
        if job.dispatch_started_at is None or (
            job.requirement.get("source"),
            job.requirement.get("version"),
        ) == ("local-speaking-acoustic", "1"):
            job.status, job.error_code = "WAITING", None
            job.claim_token, job.lease_until = None, None
            continue
        session.add(
            RawEvaluation(
                evaluation_id=uuid4(),
                job_id=job.job_id,
                submission_id=job.submission_id,
                execution_id=job.claim_token,
                status="UNKNOWN",
                input_refs=job.input_refs,
                input_fingerprint=job.input_fingerprint,
                execution_snapshot=job.execution_snapshot,
                error_code="LEASE_EXPIRED_OUTCOME_UNKNOWN",
            )
        )
        job.status, job.error_code = "UNKNOWN", "LEASE_EXPIRED_OUTCOME_UNKNOWN"
        job.claim_token, job.lease_until = None, None
    session.flush()
    return len(jobs)


def authorize_retry(session: Session, job_id: UUID, *, accept_unknown: bool = False) -> None:
    job = session.scalar(
        select(EvaluationJob)
        .where(EvaluationJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.status not in ("FAILED", "INVALID", "UNKNOWN"):
        raise ValueError("Job cannot be retried")
    if job.status == "UNKNOWN" and not accept_unknown:
        raise ValueError("Unknown external outcome requires explicit acknowledgment")
    job.status, job.error_code = "WAITING", None
    session.flush()


def run_once(
    engine: Engine,
    *,
    registry: Registry,
    adapters: Mapping[tuple[str, str], Adapter],
    lease: timedelta,
    batch_size: int,
    recording_store: RecordingStore | None = None,
    derived_recording_store: RecordingStore | None = None,
) -> dict[str, int]:
    """Scan bounded persistent batches; no in-memory queue survives a call."""
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("Batch size must be positive")
    if not isinstance(lease, timedelta) or lease <= timedelta(0):
        raise ValueError("Lease must be positive")
    with Session(engine) as s, s.begin():
        scheduled = schedule(s, limit=batch_size)
        recovered = recover_expired(s, limit=batch_size)
    with Session(engine) as s:
        ids = s.scalars(
            select(EvaluationJob.job_id)
            .where(EvaluationJob.status == "WAITING")
            .order_by(EvaluationJob.last_checked_at, EvaluationJob.job_id)
            .limit(batch_size)
        ).all()
    claimed = finished = 0
    for job_id in ids:
        try:
            work = claim_job(
                engine,
                job_id,
                registry=registry,
                adapters=adapters,
                lease=lease,
                recording_store=recording_store,
                derived_recording_store=derived_recording_store,
            )
        except InputIdentityConflict:
            continue
        except Exception:  # noqa: BLE001 - isolate a job; never persist exception text
            with Session(engine) as session, session.begin():
                context = _lock_job_context(session, job_id)
                if context is not None:
                    job, _, current = context
                    if current and job.status == "WAITING":
                        job.error_code = "INPUT_RESOLUTION_FAILED"
                        job.last_checked_at = session.scalar(select(func.clock_timestamp()))
            continue
        if work is not None:
            claimed += 1
            try:
                finished += int(execute_claim(engine, work))
            except Exception as exc:  # noqa: BLE001 - leave dispatched outcomes for UNKNOWN recovery
                with Session(engine) as session, session.begin():
                    context = _lock_job_context(session, job_id)
                    if context is not None:
                        job, _, current = context
                        if (
                            current
                            and job.status == "RUNNING"
                            and job.claim_token == work.token
                            and job.dispatch_started_at is None
                        ):
                            job.status, job.claim_token, job.lease_until = "WAITING", None, None
                            job.error_code = (
                                "INPUT_IDENTITY_CONFLICT"
                                if isinstance(exc, InputIdentityConflict)
                                else "EVALUATION_PREPARATION_FAILED"
                            )
    return {
        "scheduled": scheduled,
        "recovered": recovered,
        "claimed": claimed,
        "finished": finished,
    }
