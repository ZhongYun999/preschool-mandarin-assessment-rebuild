"""Durable scheduling and short-transaction worker operations.

Adapters are trusted deployed code: resolve_inputs only reads persisted facts and
performs no external I/O. None means an upstream dependency is not ready. The
adapter must validate the versioned output contract, and execute outside the DB
transaction. No production adapters are registered here.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.papers.paper import EvaluationPlan, PaperEntry
from app.scoring.registry import Registry
from app.sessions.assessment_session import SessionTask
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

    def __init__(self, code: str):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
            raise ValueError("Invalid diagnostic code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    token: UUID
    inputs: InputSnapshot
    requirement: dict
    adapter: Adapter


def _json(value: Any) -> str:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def schedule(session: Session) -> int:
    """Create at most one job per genuine, frozen intent in the caller transaction."""
    rows = session.execute(
        select(EvaluationIntent, Submission, SessionTask, EvaluationPlan)
        .join(Submission, Submission.submission_id == EvaluationIntent.submission_id)
        .join(SessionTask, SessionTask.session_task_id == Submission.session_task_id)
        .join(EvaluationPlan, EvaluationPlan.paper_entry_id == EvaluationIntent.paper_entry_id)
    ).all()
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
            or set(requirement) != {"source", "version", "parameters"}
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
    return count


def claim_job(
    engine: Engine,
    job_id: UUID,
    *,
    registry: Registry,
    adapters: Mapping[tuple[str, str], Adapter],
    lease: timedelta,
) -> Claim | None:
    if not isinstance(lease, timedelta) or lease <= timedelta(0):
        raise ValueError("Lease must be positive")
    with Session(engine) as s, s.begin():
        job = s.scalar(
            select(EvaluationJob)
            .where(EvaluationJob.job_id == job_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if job is None or job.status != "WAITING":
            return None
        submission = s.get(Submission, job.submission_id)
        entry = s.get(PaperEntry, job.paper_entry_id)
        revision = s.get(TaskRevision, entry.task_revision_id)
        adapter = adapters.get((job.requirement["source"], job.requirement["version"]))
        if adapter is None or not registry.supports(revision.task_type, job.requirement):
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
        refs = {
            "submission_id": str(submission.submission_id),
            "task_revision_id": str(revision.task_revision_id),
            "paper_entry_id": str(entry.paper_entry_id),
            "dependencies": inputs.refs,
        }
        digest = hashlib.sha256(
            _json(
                {"refs": refs, "payload": inputs.payload, "requirement": job.requirement}
            ).encode()
        ).hexdigest()
        if job.input_fingerprint is not None and (
            job.input_fingerprint != digest or job.input_refs != refs
        ):
            raise ValueError("Evaluation inputs changed after first claim")
        now = s.scalar(select(func.clock_timestamp()))
        token = uuid4()
        job.input_refs, job.input_fingerprint = refs, digest
        job.claim_token, job.lease_until = token, now + lease
        job.status, job.error_code = "RUNNING", None
        job.attempts += 1
        s.flush()
        return Claim(job.job_id, token, inputs, json.loads(_json(job.requirement)), adapter)


def _finish(
    engine: Engine,
    claim: Claim,
    *,
    status: str,
    result: AdapterResult | None = None,
    error_code: str | None = None,
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
        s.add(
            RawEvaluation(
                evaluation_id=uuid4(),
                job_id=job.job_id,
                submission_id=job.submission_id,
                execution_id=claim.token,
                status=status,
                input_refs=job.input_refs,
                input_fingerprint=job.input_fingerprint,
                provider=result.provider if result else None,
                model=result.model if result else None,
                engine_version=result.engine_version if result else None,
                payload=result.payload if status == "READY" else None,
                error_code=error_code,
            )
        )
        job.status, job.error_code = status, error_code
        job.claim_token, job.lease_until = None, None
        s.flush()
        return True


def execute_claim(engine: Engine, claim: Claim) -> bool:
    """Invoke outside any DB transaction; unexpected external outcomes remain unknown."""
    with Session(engine) as s:
        live = s.scalar(
            select(EvaluationJob).where(
                EvaluationJob.job_id == claim.job_id,
                EvaluationJob.status == "RUNNING",
                EvaluationJob.claim_token == claim.token,
                EvaluationJob.lease_until > func.clock_timestamp(),
            )
        )
        if live is None:
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
    inputs = InputSnapshot(
        json.loads(_json(claim.inputs.refs)), json.loads(_json(claim.inputs.payload))
    )
    requirement = json.loads(_json(claim.requirement))
    try:
        result = claim.adapter.evaluate(inputs, requirement)
    except KnownFailure as exc:
        return _finish(engine, claim, status="FAILED", error_code=exc.code)
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


def recover_expired(session: Session) -> int:
    """An expired execution may already have called an external provider."""
    jobs = session.scalars(
        select(EvaluationJob)
        .where(
            EvaluationJob.status == "RUNNING", EvaluationJob.lease_until <= func.clock_timestamp()
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for job in jobs:
        session.add(
            RawEvaluation(
                evaluation_id=uuid4(),
                job_id=job.job_id,
                submission_id=job.submission_id,
                execution_id=job.claim_token,
                status="UNKNOWN",
                input_refs=job.input_refs,
                input_fingerprint=job.input_fingerprint,
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
