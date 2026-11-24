"""Assessment session identity and current input facts, without mutations."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.assessment_auth import assessment_read_db, require_read_session_access
from app.media.fixed_audio import FixedAudio
from app.media.recording import Recording
from app.papers.paper import PaperEntry
from app.scoring.models import FinalScoreResult
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.sessions.submission import Playback, Submission, _busy, _completed
from app.task_types.speaking import SpeakingSegment
from app.task_types.speaking_asr import SpeakingASR
from app.task_types.speaking_guidance import active_help
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
    guide_complete: bool
    initial_prompt_complete: bool
    playback_in_progress: bool
    recording_in_progress: bool
    saved_recording_ids: list[UUID]
    allowed_actions: list[str]
    speaking: dict | None = None


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
        recordings = (
            list(db.scalars(select(Recording).where(Recording.attempt_id == attempt.attempt_id)))
            if attempt and task_type in ("repetition", "qa", "speaking")
            else []
        )
        recording_in_progress = any(r.status == "STARTED" for r in recordings)
        saved_recording_ids = [r.recording_id for r in recordings if r.status == "SAVED"]
        guide_complete = bool(
            attempt and (not guide_text or _completed(db, attempt.attempt_id, "GUIDE"))
        )
        initial_complete = bool(attempt and _completed(db, attempt.attempt_id, "INITIAL_PROMPT"))
        playback_in_progress = bool(attempt and _busy(db, attempt.attempt_id))
        segments = (
            list(
                db.scalars(
                    select(SpeakingSegment)
                    .where(SpeakingSegment.attempt_id == attempt.attempt_id)
                    .order_by(SpeakingSegment.position)
                )
            )
            if attempt and task_type == "speaking"
            else []
        )
        recognition = (
            {
                row.segment_id: row
                for row in db.scalars(
                    select(SpeakingASR).where(SpeakingASR.attempt_id == attempt.attempt_id)
                )
            }
            if attempt and task_type == "speaking"
            else {}
        )
        pending_asr = [
            recognition.get(segment.segment_id)
            for segment in segments
            if recognition.get(segment.segment_id) is None
            or recognition[segment.segment_id].status != "READY"
        ]
        help_request = (
            active_help(db, attempt.attempt_id) if attempt and task_type == "speaking" else None
        )
        actions = []
        if owner.status == "IN_PROGRESS":
            if attempt is None:
                actions = ["START_ATTEMPT"]
            elif attempt.status == "OPEN" and task_type in ("instruction", "repetition", "qa"):
                if task_type in ("repetition", "qa") and recording_in_progress:
                    actions = ["UPLOAD_RECORDING", "CANCEL_RECORDING", "REDO_ATTEMPT"]
                else:
                    actions = _instruction_actions(db, attempt, guide_text, task.paper_entry_id)
                    if task_type in ("repetition", "qa") and "SUBMIT_INSTRUCTION" in actions:
                        actions.remove("SUBMIT_INSTRUCTION")
                        actions.append("START_RECORDING")
                        if saved_recording_ids:
                            actions.append(
                                "SUBMIT_REPETITION" if task_type == "repetition" else "SUBMIT_QA"
                            )
            elif attempt.status == "OPEN" and task_type == "speaking":
                actions = _instruction_actions(db, attempt, guide_text, task.paper_entry_id)
                actions = [
                    a for a in actions if a not in ("START_USER_REPLAY", "SUBMIT_INSTRUCTION")
                ]
                if help_request is not None:
                    actions = ["REDO_ATTEMPT"]
                    if help_request.status in ("FAILED", "UNKNOWN"):
                        actions.extend(["RETRY_HELP", "VOID_HELP"])
                elif recording_in_progress:
                    actions = ["UPLOAD_RECORDING", "CANCEL_RECORDING", "REDO_ATTEMPT"]
                elif (
                    guide_complete
                    and initial_complete
                    and not playback_in_progress
                    and not pending_asr
                ):
                    actions.append("START_RECORDING")
                    actions.append("REQUEST_HELP")
                if (
                    pending_asr
                    and pending_asr[-1] is not None
                    and pending_asr[-1].status in ("FAILED", "UNKNOWN")
                ):
                    actions.append("RETRY_CURRENT_ASR")
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
                guide_complete=guide_complete,
                initial_prompt_complete=initial_complete,
                playback_in_progress=playback_in_progress,
                recording_in_progress=recording_in_progress,
                saved_recording_ids=saved_recording_ids,
                allowed_actions=actions,
                speaking={
                    "phase": "DELIVERING_GUIDANCE"
                    if help_request is not None and help_request.status == "READY"
                    else "RECOVERABLE_ERROR"
                    if help_request is not None and help_request.status in ("FAILED", "UNKNOWN")
                    else "GENERATING_GUIDANCE"
                    if help_request is not None
                    else "RECOVERABLE_ERROR"
                    if pending_asr
                    and pending_asr[-1] is not None
                    and pending_asr[-1].status in ("FAILED", "UNKNOWN")
                    else "ANALYZING_AUDIO"
                    if pending_asr
                    else "RECEIVING_AUDIO"
                    if recording_in_progress
                    else "WAITING_INPUT"
                    if guide_complete and initial_complete
                    else "DELIVERING_INITIAL",
                    "checkpoint_a": bool(segments),
                    "asr_status": recognition[segments[-1].segment_id].status
                    if segments and segments[-1].segment_id in recognition
                    else None,
                    "checkpoint_b": bool(segments) and not pending_asr,
                    "help_request": {
                        "help_request_id": help_request.help_request_id,
                        "status": help_request.status,
                        "input_frontier": help_request.input_frontier,
                        "checkpoint_c": help_request.status == "READY",
                        "guidance_text": help_request.guidance_text,
                        "error_code": help_request.error_code,
                        "attempts": help_request.attempts,
                    }
                    if help_request is not None
                    else None,
                    "pending_recording_ids": [
                        r.recording_id for r in recordings if r.status == "STARTED"
                    ],
                    "total_duration_seconds": sum(s.duration_seconds for s in segments),
                    "segments": [
                        {
                            "segment_id": s.segment_id,
                            "recording_id": s.recording_id,
                            "position": s.position,
                            "duration_seconds": s.duration_seconds,
                            "asr": {
                                "operation_id": recognition[s.segment_id].operation_id,
                                "status": recognition[s.segment_id].status,
                                "semantic": recognition[s.segment_id].semantic,
                                "text": recognition[s.segment_id].transcript,
                                "error_code": recognition[s.segment_id].error_code,
                                "attempts": recognition[s.segment_id].attempts,
                            }
                            if s.segment_id in recognition
                            else None,
                        }
                        for s in segments
                    ],
                }
                if attempt and task_type == "speaking"
                else None,
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
