"""Explicit worker for Speaking guidance; no external calls on page access."""

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import create_database_engine
from app.evaluation.qwen_qa_text import _failure, _invalid_number, _unique_object
from app.evaluation.worker import KnownFailure
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.task_types.speaking_guidance import SpeakingHelpRequest
from app.taskbank.models import TaskRevision


@dataclass(frozen=True)
class Claim:
    help_request_id: UUID
    token: UUID
    prompt: str
    contract: dict
    context: dict


def _locks(s, help_id):
    identity = s.execute(
        select(SpeakingHelpRequest.attempt_id, Attempt.session_task_id, SessionTask.session_id)
        .join(Attempt, Attempt.attempt_id == SpeakingHelpRequest.attempt_id)
        .join(SessionTask, SessionTask.session_task_id == Attempt.session_task_id)
        .where(SpeakingHelpRequest.help_request_id == help_id)
    ).one_or_none()
    if identity is None:
        return None
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
        select(SpeakingHelpRequest)
        .where(SpeakingHelpRequest.help_request_id == help_id)
        .with_for_update()
    )
    return owner, task, attempt, job


def claim(engine, help_id, *, prompt, contract, lease=timedelta(minutes=5)):
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or not isinstance(contract, dict)
        or set(contract) != {"provider", "model", "region", "workspace"}
        or not all(isinstance(v, str) and v.strip() for v in contract.values())
        or lease <= timedelta(0)
    ):
        raise ValueError("Invalid guidance configuration")
    with Session(engine) as s, s.begin():
        locked = _locks(s, help_id)
        if locked is None:
            return None
        owner, task, attempt, job = locked
        if (
            owner.status != "IN_PROGRESS"
            or task.current_attempt_id != attempt.attempt_id
            or attempt.status != "OPEN"
            or job.status != "WAITING"
        ):
            return None
        revision = s.get(TaskRevision, UUID(job.input_frontier["task_revision_id"]))
        if revision is None:
            job.status, job.error_code = "FAILED", "GUIDANCE_INPUT_UNAVAILABLE"
            return None
        context = {
            "prompt_text": revision.prompt_text,
            "grade_level": revision.grade_level,
            "material_description": revision.material_description,
            "ai_followup_scope": revision.ai_followup_scope,
            "segments": job.input_frontier["segments"],
            "delivered_guidance": job.input_frontier["delivered_guidance"],
        }
        now = s.scalar(select(func.clock_timestamp()))
        token = uuid4()
        job.prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        job.contract = contract
        job.status, job.claim_token, job.lease_until = "RUNNING", token, now + lease
        job.dispatched_at, job.error_code, job.attempts = None, None, job.attempts + 1
        return Claim(job.help_request_id, token, prompt, contract, context)


def execute(engine, work, adapter):
    with Session(engine) as s, s.begin():
        locked = _locks(s, work.help_request_id)
        if locked is None:
            return False
        owner, task, attempt, job = locked
        now = s.scalar(select(func.clock_timestamp()))
        if (
            job.status != "RUNNING"
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
        revision = s.get(TaskRevision, UUID(job.input_frontier["task_revision_id"]))
        expected = {
            "prompt_text": revision.prompt_text,
            "grade_level": revision.grade_level,
            "material_description": revision.material_description,
            "ai_followup_scope": revision.ai_followup_scope,
            "segments": job.input_frontier["segments"],
            "delivered_guidance": job.input_frontier["delivered_guidance"],
        }
        if (
            job.contract != work.contract
            or job.prompt_sha256 != hashlib.sha256(work.prompt.encode("utf-8")).hexdigest()
            or work.context != expected
        ):
            job.execution_history = [
                *job.execution_history,
                {
                    "attempt": job.attempts,
                    "execution_id": str(work.token),
                    "status": "FAILED",
                    "error_code": "GUIDANCE_INPUT_MISMATCH",
                    "prompt_sha256": job.prompt_sha256,
                    "contract": job.contract,
                },
            ]
            job.status, job.error_code, job.claim_token, job.lease_until = (
                "FAILED",
                "GUIDANCE_INPUT_MISMATCH",
                None,
                None,
            )
            return False
        job.dispatched_at = now
    try:
        output = adapter.generate(work.prompt, work.context, work.contract)
        if (
            not isinstance(output, dict)
            or not isinstance(output.get("guidance_text"), str)
            or not output["guidance_text"].strip()
        ):
            raise KnownFailure("GUIDANCE_INVALID_RESPONSE")
        status, error = "READY", None
    except KnownFailure as exc:
        output, status, error = None, "FAILED", exc.code
    except Exception:  # noqa: BLE001 - external outcome unknown
        output, status, error = None, "UNKNOWN", "EXTERNAL_OUTCOME_UNKNOWN"
    with Session(engine) as s, s.begin():
        job = s.scalar(
            select(SpeakingHelpRequest)
            .where(SpeakingHelpRequest.help_request_id == work.help_request_id)
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
        job.status, job.error_code = status, error
        if output is not None:
            job.guidance_text = output["guidance_text"]
            job.provider, job.model = work.contract.get("provider"), work.contract.get("model")
            request_id = output.get("request_id")
            job.provider_request_id = (
                request_id
                if isinstance(request_id, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)
                else None
            )
        job.execution_history = [
            *job.execution_history,
            {
                "attempt": job.attempts,
                "execution_id": str(work.token),
                "status": status,
                "error_code": error,
                "prompt_sha256": job.prompt_sha256,
                "contract": job.contract,
                "provider_request_id": job.provider_request_id if output is not None else None,
            },
        ]
        job.claim_token, job.lease_until = None, None
        return True


def recover_expired(session):
    now = session.scalar(select(func.clock_timestamp()))
    jobs = session.scalars(
        select(SpeakingHelpRequest)
        .where(SpeakingHelpRequest.status == "RUNNING", SpeakingHelpRequest.lease_until <= now)
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        job.status = "UNKNOWN" if job.dispatched_at else "WAITING"
        job.error_code = "LEASE_EXPIRED_OUTCOME_UNKNOWN" if job.dispatched_at else None
        job.execution_history = [
            *job.execution_history,
            {
                "attempt": job.attempts,
                "execution_id": str(job.claim_token),
                "status": job.status,
                "error_code": job.error_code,
                "prompt_sha256": job.prompt_sha256,
                "contract": job.contract,
            },
        ]
        job.claim_token, job.lease_until = None, None
    return len(jobs)


def fail_configuration(engine, help_id):
    """Record a definite local configuration failure without an external call."""
    with Session(engine) as s, s.begin():
        locked = _locks(s, help_id)
        if locked is None:
            return False
        owner, task, attempt, job = locked
        if (
            owner.status != "IN_PROGRESS"
            or task.current_attempt_id != attempt.attempt_id
            or attempt.status != "OPEN"
            or job.status != "WAITING"
        ):
            return False
        job.status, job.error_code = "FAILED", "GUIDANCE_CONFIGURATION_UNAVAILABLE"
        job.execution_history = [
            *job.execution_history,
            {
                "attempt": job.attempts,
                "status": "FAILED",
                "error_code": job.error_code,
            },
        ]
        return True


class QwenGuidance:
    def __init__(self, *, client_factory=None, timeout=60):
        self.factory, self.timeout = client_factory, timeout

    def generate(self, prompt, context, contract):
        import dashscope
        from dashscope import errors

        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise KnownFailure("LLM_CREDENTIALS_MISSING")
        factory = self.factory or dashscope.Client
        try:
            with factory(
                api_key=key,
                base_url=f"https://{contract['workspace']}.{contract['region']}.maas.aliyuncs.com/api/v1",
                region=contract["region"],
                workspace=contract["workspace"],
                max_retries=0,
                timeout=self.timeout,
                raise_on_error=False,
            ) as client:
                response = client.chat.completions.create(
                    model=contract["model"],
                    messages=[
                        {"role": "system", "content": prompt},
                        {
                            "role": "user",
                            "content": json.dumps(context, ensure_ascii=False, allow_nan=False),
                        },
                    ],
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    stream=False,
                    max_retries=0,
                    raw=True,
                )
        except errors.AuthenticationError:
            raise KnownFailure("LLM_AUTHENTICATION") from None
        except errors.PermissionDeniedError as exc:
            raise KnownFailure(
                "LLM_QUOTA" if exc.code in {"Arrearage", "insufficient_quota"} else "LLM_PERMISSION"
            ) from None
        except errors.BadRequestError:
            raise KnownFailure("LLM_REQUEST_REJECTED") from None
        except errors.RateLimitError as exc:
            raise KnownFailure(
                "LLM_QUOTA"
                if exc.code in {"insufficient_quota", "Throttling.AllocationQuota"}
                else "LLM_RATE_LIMIT"
            ) from None
        except (errors.APITimeoutError, errors.APIConnectionError):
            raise RuntimeError("LLM external outcome unknown") from None
        status = response.status_code
        if status >= 500 or status in (408, 409):
            raise RuntimeError("LLM external outcome unknown")
        try:
            body = response.http_response.json()
            code = body.get("code") if isinstance(body, dict) else None
            if status != 200 or code:
                reason = _failure(status, code)
                if reason:
                    raise KnownFailure(reason)
                raise RuntimeError("LLM external outcome unknown")
            choices = body["output"]["choices"]
            if (
                len(choices) != 1
                or choices[0]["finish_reason"] != "stop"
                or choices[0]["message"]["role"] != "assistant"
            ):
                raise TypeError
            value = json.loads(
                choices[0]["message"]["content"],
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_number,
            )
            if not isinstance(value, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError, IndexError):
            reason = _failure(status, None)
            if reason:
                raise KnownFailure(reason) from None
            raise KnownFailure("GUIDANCE_INVALID_RESPONSE") from None
        value["request_id"] = body.get("request_id") or response.request_id
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("help_request_id", type=UUID)
    parser.add_argument("--region", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    engine = create_database_engine()
    try:
        try:
            if (
                args.region not in {"cn-beijing", "ap-southeast-1", "us-east-1"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", args.workspace)
                or not args.model.strip()
            ):
                raise ValueError("Invalid provider configuration")
            prompt_path = os.environ.get("SPEAKING_GUIDANCE_PROMPT_FILE")
            if not prompt_path:
                raise ValueError("Missing prompt configuration")
            prompt = Path(prompt_path).read_text(encoding="utf-8")
            if not prompt.strip():
                raise ValueError("Empty prompt configuration")
        except (ValueError, OSError, UnicodeError):
            fail_configuration(engine, args.help_request_id)
            raise ValueError("Guidance configuration unavailable") from None
        with Session(engine) as s, s.begin():
            recover_expired(s)
        work = claim(
            engine,
            args.help_request_id,
            prompt=prompt,
            contract={
                "provider": "dashscope",
                "model": args.model,
                "region": args.region,
                "workspace": args.workspace,
            },
        )
        if work is None:
            raise ValueError("Help request is not available")
        execute(engine, work, QwenGuidance())
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
