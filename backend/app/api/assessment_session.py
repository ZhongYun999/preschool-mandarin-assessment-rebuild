"""Assessment session identity and current input facts, without mutations."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.assessment_auth import assessment_read_db, require_read_session_access
from app.media.fixed_audio import FixedAudio
from app.papers.paper import PaperEntry
from app.scoring.models import FinalScoreResult
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.sessions.submission import Playback, Submission, _busy, _completed
from app.taskbank.models import TaskRevision

router = APIRouter(prefix="/assessment")
authorized_dependency = Depends(require_read_session_access)
read_db_dependency = Depends(assessment_read_db)


class TaskState(BaseModel):
    session_task_id: UUID
    paper_entry_id: UUID
    position: int
    task_type: str
    attempt_id: UUID | None
    attempt_status: str | None
    submission_exists: bool
    allowed_actions: list[str]


class SessionState(BaseModel):
    session_id: UUID
    paper_id: UUID
    status: str
    input_closed: bool
    final_result_available: bool
    allowed_actions: list[str]
    tasks: list[TaskState]


def _instruction_actions(db, attempt, guide_text, entry_id):
    actions = ["REDO_ATTEMPT"]
    if _busy(db, attempt.attempt_id):
        return ["FINISH_PLAYBACK", *actions]
    guide_done = not guide_text or _completed(db, attempt.attempt_id, "GUIDE")
    prompt_done = _completed(db, attempt.attempt_id, "INITIAL_PROMPT")
    ready_roles = set(
        db.scalars(
            select(FixedAudio.role).where(
                FixedAudio.paper_entry_id == entry_id, FixedAudio.status == "READY"
            )
        )
    )
    if not guide_done and not prompt_done:
        if "guide" in ready_roles:
            actions.append("START_GUIDE")
    elif guide_done and not prompt_done:
        if "prompt" in ready_roles:
            actions.append("START_INITIAL_PROMPT")
    elif guide_done and prompt_done:
        if "prompt" in ready_roles:
            actions.append("START_USER_REPLAY")
        actions.append("SUBMIT_INSTRUCTION")
    rows = list(db.scalars(select(Playback).where(Playback.attempt_id == attempt.attempt_id)))
    completed_retries = {
        p.retry_of_id for p in rows if p.kind == "TECHNICAL_RETRY" and p.status == "COMPLETED"
    }
    if any(
        p.status == "FAILED"
        and ("guide" if p.kind == "GUIDE" else "prompt") in ready_roles
        and p.playback_id not in completed_retries
        and (
            p.kind == "USER_REPLAY"
            or (p.kind == "GUIDE" and not guide_done)
            or (p.kind == "INITIAL_PROMPT" and not prompt_done)
        )
        for p in rows
    ):
        actions.append("RETRY_PLAYBACK")
    return actions


@router.get("/sessions/{session_id}", response_model=SessionState)
def session_state(
    session_id: UUID,
    response: Response,
    authorized: UUID = authorized_dependency,
    db: Session = read_db_dependency,
):
    owner = db.get(AssessmentSession, authorized)
    rows = db.execute(
        select(SessionTask, PaperEntry.position, TaskRevision.task_type, TaskRevision.guide_text)
        .join(PaperEntry, PaperEntry.paper_entry_id == SessionTask.paper_entry_id)
        .join(TaskRevision, TaskRevision.task_revision_id == PaperEntry.task_revision_id)
        .where(SessionTask.session_id == session_id)
        .order_by(PaperEntry.position, PaperEntry.paper_entry_id)
    ).all()
    tasks = []
    for task, position, task_type, guide_text in rows:
        attempt = db.get(Attempt, task.current_attempt_id) if task.current_attempt_id else None
        submission = bool(
            attempt
            and db.scalar(
                select(Submission.submission_id).where(
                    Submission.attempt_id == attempt.attempt_id,
                    Submission.session_task_id == task.session_task_id,
                )
            )
        )
        actions = []
        if owner.status == "IN_PROGRESS":
            if attempt is None:
                actions = ["START_ATTEMPT"]
            elif attempt.status == "OPEN" and task_type == "instruction":
                actions = _instruction_actions(db, attempt, guide_text, task.paper_entry_id)
            else:
                actions = ["REDO_ATTEMPT"]
        tasks.append(
            TaskState(
                session_task_id=task.session_task_id,
                paper_entry_id=task.paper_entry_id,
                position=position,
                task_type=task_type,
                attempt_id=attempt.attempt_id if attempt else None,
                attempt_status=attempt.status if attempt else None,
                submission_exists=submission,
                allowed_actions=actions,
            )
        )
    expected_entries = set(
        db.scalars(select(PaperEntry.paper_entry_id).where(PaperEntry.paper_id == owner.paper_id))
    )
    can_close = (
        owner.status == "IN_PROGRESS"
        and expected_entries == {t.paper_entry_id for t in tasks}
        and all(t.attempt_status == "SEALED" and t.submission_exists for t in tasks)
    )
    final_exists = bool(
        db.scalar(
            select(FinalScoreResult.result_id).where(
                FinalScoreResult.session_id == owner.session_id
            )
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return SessionState(
        session_id=owner.session_id,
        paper_id=owner.paper_id,
        status=owner.status,
        input_closed=owner.status != "IN_PROGRESS",
        final_result_available=final_exists,
        allowed_actions=["CLOSE_SESSION"] if can_close else [],
        tasks=tasks,
    )
