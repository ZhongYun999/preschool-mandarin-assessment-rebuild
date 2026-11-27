"""Read and validate the frozen upstream recognition evidence."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation.identity import InputIdentityConflict
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.source_identity import _recording_identity, valid_speaking_consumption
from app.papers.paper import EvaluationPlan
from app.scoring.registry import Registry
from app.sessions.submission import Submission
from app.taskbank.models import TaskRevision


def upstream_asr(
    session: Session,
    job: EvaluationJob,
    submission: Submission,
    revision: TaskRevision,
    plan: EvaluationPlan,
    registry: Registry,
    speaking_input: dict | None = None,
):
    dependency = job.requirement.get("depends_on")
    if dependency is None:
        return None
    index = dependency["requirement_index"]
    expected = plan.requirements[index]
    upstream = session.scalar(
        select(EvaluationJob).where(
            EvaluationJob.submission_id == submission.submission_id,
            EvaluationJob.paper_entry_id == job.paper_entry_id,
            EvaluationJob.requirement_index == index,
        )
    )
    if upstream is None or upstream.status != "READY":
        return None
    raw = session.scalar(
        select(RawEvaluation).where(
            RawEvaluation.job_id == upstream.job_id, RawEvaluation.status == "READY"
        )
    )
    refs = upstream.input_refs or {}
    recording = _recording_identity(session, submission) if speaking_input is None else None
    if (
        upstream.requirement != expected
        or raw is None
        or raw.submission_id != submission.submission_id
        or raw.input_refs != upstream.input_refs
        or raw.input_fingerprint != upstream.input_fingerprint
        or refs.get("submission_id") != str(submission.submission_id)
        or refs.get("paper_entry_id") != str(job.paper_entry_id)
        or refs.get("task_revision_id") != str(revision.task_revision_id)
        or not isinstance(refs.get("dependencies"), dict)
        or (
            recording is not None
            and any(refs["dependencies"].get(k) != v for k, v in recording.items())
        )
        or (
            speaking_input is not None
            and (
                refs.get("speaking_source") != speaking_input
                or not valid_speaking_consumption(
                    registry, expected, speaking_input, refs["dependencies"]
                )
            )
        )
    ):
        raise InputIdentityConflict("Upstream recognition evidence identity mismatch")
    result = registry.recognition_result(
        expected,
        raw.payload,
        speaking_source=speaking_input,
        speaking_consumption=refs["dependencies"].get("speaking_consumption"),
        provider=raw.provider,
        model=raw.model,
    )
    if result["status"] == "NO_TEXT" and not registry.accepts_no_text(job.requirement):
        if job.status == "WAITING":
            job.error_code = "ASR_NO_TEXT_NOT_ACCEPTED"
        return None
    identity = {
        "job_id": str(upstream.job_id),
        "evaluation_id": str(raw.evaluation_id),
        "source": expected["source"],
        "version": expected["version"],
        "input_fingerprint": raw.input_fingerprint,
        "provider": raw.provider,
        "model": raw.model,
        "engine_version": raw.engine_version,
        "submission_id": str(submission.submission_id),
        "paper_entry_id": str(job.paper_entry_id),
        "task_revision_id": str(revision.task_revision_id),
        **(
            recording
            if recording is not None
            else {
                "speaking_source_sha256": speaking_input["sha256"],
                "speaking_consumption": refs["dependencies"]["speaking_consumption"],
            }
        ),
    }
    return identity, result
