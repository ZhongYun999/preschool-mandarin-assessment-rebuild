"""Assessment session identity and current input facts, without mutations."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.assessment_auth import assessment_read_db, require_read_session_access
from app.api.assessment_repetition import get_recording_store
from app.api.teacher_api import get_audio_store
from app.media.fixed_audio import AudioStore, FixedAudio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import PaperEntry
from app.scoring.models import FinalScoreResult
from app.sessions.assessment_session import AssessmentSession, Attempt, SessionTask
from app.sessions.submission import Playback, Submission, _busy, _completed, effective_replay_ids
from app.task_types.speaking import (
    SpeakingSegment,
    finish_qualification,
    replay_frozen_resource,
    replay_target,
)
from app.task_types.speaking_asr import SpeakingASR
from app.task_types.speaking_guidance import GuidanceAudio, SpeakingHelpRequest, active_help
from app.taskbank.models import TaskRevision

router = APIRouter(prefix="/assessment")
authorized_dependency = Depends(require_read_session_access)
read_db_dependency = Depends(assessment_read_db)
audio_store_dependency = Depends(get_audio_store)
recording_store_dependency = Depends(get_recording_store)


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
    store: AudioStore = audio_store_dependency,
    recording_store: RecordingStore = recording_store_dependency,
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
        submission_id = (
            attempt
            and db.scalar(
                select(Submission.submission_id).where(
                    Submission.attempt_id == attempt.attempt_id,
                    Submission.session_task_id == task.session_task_id,
                )
            )
        ) or None
        submission = submission_id is not None
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
        audio = (
            db.scalar(
                select(GuidanceAudio).where(
                    GuidanceAudio.help_request_id == help_request.help_request_id
                )
            )
            if help_request
            else None
        )
        guidance_playbacks = (
            list(db.scalars(select(Playback).where(Playback.guidance_audio_id == audio.audio_id)))
            if audio
            else []
        )
        delivered = (
            list(
                db.scalars(
                    select(SpeakingHelpRequest)
                    .where(
                        SpeakingHelpRequest.attempt_id == attempt.attempt_id,
                        SpeakingHelpRequest.status == "DELIVERED",
                        SpeakingHelpRequest.source == "child_requested",
                    )
                    .order_by(SpeakingHelpRequest.position)
                )
            )
            if attempt and task_type == "speaking"
            else []
        )
        last_delivered_audio = (
            db.scalar(
                select(GuidanceAudio).where(
                    GuidanceAudio.help_request_id == delivered[-1].help_request_id
                )
            )
            if delivered
            else None
        )
        qualification = None
        if attempt and task_type == "speaking":
            entry = db.get(PaperEntry, task.paper_entry_id)
            revision = db.get(TaskRevision, entry.task_revision_id)
            qualification = finish_qualification(db, task, attempt, revision, recording_store)
        replay_source, replay_audio, replay_help = (None, None, None)
        replay_available = False
        replay_rows, active_replay, replay_retry_ids = [], None, []
        if attempt and task_type == "speaking":
            replay_source, replay_audio, replay_help = replay_target(db, task, attempt.attempt_id)
            if replay_audio is not None:
                target = Playback(
                    audio_id=replay_audio.audio_id if replay_source == "INITIAL_PROMPT" else None,
                    guidance_audio_id=replay_audio.audio_id
                    if replay_source == "GUIDANCE"
                    else None,
                )
                try:
                    replay_frozen_resource(db, task, attempt.attempt_id, target, store)
                    replay_available = True
                except ValueError:
                    pass
            playback_rows = list(
                db.scalars(select(Playback).where(Playback.attempt_id == attempt.attempt_id))
            )
            roots = {p.playback_id: p for p in playback_rows if p.kind == "USER_REPLAY"}
            replay_rows = [
                p
                for p in playback_rows
                if p.kind == "USER_REPLAY"
                or (p.kind == "TECHNICAL_RETRY" and p.retry_of_id in roots)
            ]
            active_replay = next((p for p in replay_rows if p.status == "STARTED"), None)
            completed_ids = set(effective_replay_ids(db, attempt.attempt_id))
            for original in roots.values():
                if original.status != "FAILED" or original.playback_id in completed_ids:
                    continue
                try:
                    replay_frozen_resource(db, task, attempt.attempt_id, original, store)
                    replay_retry_ids.append(original.playback_id)
                except ValueError:
                    pass
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
                if guide_complete and initial_complete:
                    actions = [a for a in actions if a != "RETRY_PLAYBACK"]
                if help_request is not None:
                    actions = ["REDO_ATTEMPT"]
                    if help_request.status in ("FAILED", "UNKNOWN"):
                        actions.extend(["RETRY_HELP", "VOID_HELP"])
                    if audio and audio.status in ("FAILED", "UNKNOWN"):
                        actions.extend(["RETRY_GUIDANCE_TTS", "VOID_HELP"])
                    if audio and audio.status == "READY":
                        if any(p.status == "STARTED" for p in guidance_playbacks):
                            actions.append("FINISH_PLAYBACK")
                        elif guidance_playbacks:
                            actions.extend(["RETRY_GUIDANCE_PLAYBACK", "VOID_HELP"])
                        else:
                            actions.append("START_GUIDANCE_PLAYBACK")
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
                    if replay_available:
                        actions.append("START_USER_REPLAY")
                    if replay_retry_ids:
                        actions.append("RETRY_PLAYBACK")
                    if qualification["eligible"]:
                        actions.append("FINISH_SPEAKING")
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
                    "phase": "SEALED"
                    if attempt.status == "SEALED"
                    else "DELIVERING_GUIDANCE"
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
                    else "REPLAYING_PROMPT"
                    if active_replay is not None
                    else "WAITING_INPUT"
                    if guide_complete and initial_complete
                    else "DELIVERING_INITIAL",
                    "checkpoint_a": bool(segments),
                    "minimum_duration_seconds": qualification["minimum"],
                    "minimum_duration_met": qualification["minimum_met"],
                    "finish_eligible": qualification["eligible"],
                    "finish_unavailable_reason": qualification["reason"],
                    "submission_id": submission_id,
                    "submission_status": "ACCEPTED" if submission else None,
                    "asr_status": recognition[segments[-1].segment_id].status
                    if segments and segments[-1].segment_id in recognition
                    else None,
                    "checkpoint_b": bool(segments) and not pending_asr,
                    "replay_count": len(effective_replay_ids(db, attempt.attempt_id)),
                    "replay_target": {
                        "source": replay_source,
                        "audio_id": replay_audio.audio_id if replay_audio else None,
                        "help_request_id": replay_help.help_request_id if replay_help else None,
                        "available": replay_available,
                    },
                    "active_replay": {
                        "playback_id": active_replay.playback_id,
                        "kind": active_replay.kind,
                        "status": active_replay.status,
                        "retry_of_id": active_replay.retry_of_id,
                    }
                    if active_replay
                    else None,
                    "replay_playbacks": [
                        {
                            "playback_id": p.playback_id,
                            "request_id": p.request_id,
                            "kind": p.kind,
                            "status": p.status,
                            "retry_of_id": p.retry_of_id,
                            "audio_id": p.audio_id,
                            "guidance_audio_id": p.guidance_audio_id,
                        }
                        for p in replay_rows
                    ],
                    "replay_retry_ids": replay_retry_ids if "RETRY_PLAYBACK" in actions else [],
                    "effective_help_count": len(delivered),
                    "latest_delivered_help_request_id": delivered[-1].help_request_id
                    if delivered
                    else None,
                    "latest_delivered_guidance": {
                        "help_request_id": delivered[-1].help_request_id,
                        "checkpoint_c": True,
                        "checkpoint_d": True,
                        "audio_id": last_delivered_audio.audio_id if last_delivered_audio else None,
                    }
                    if delivered
                    else None,
                    "checkpoint_d": bool(delivered) and help_request is None,
                    "guidance_audio": {
                        "audio_id": audio.audio_id,
                        "status": audio.status,
                        "error_code": audio.error_code,
                        "attempts": audio.attempts,
                        "audio_url": f"/assessment/sessions/{session_id}/tasks/{task.session_task_id}/guidance-audio/{audio.audio_id}"
                        if audio.status == "READY"
                        else None,
                        "playbacks": [
                            {
                                "playback_id": p.playback_id,
                                "kind": p.kind,
                                "status": p.status,
                                "retry_of_id": p.retry_of_id,
                            }
                            for p in guidance_playbacks
                        ],
                    }
                    if audio
                    else None,
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
                    "total_duration_seconds": qualification["total"],
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
