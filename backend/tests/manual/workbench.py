"""Loopback-only, offline QA workbench. Never mounted by the production app."""

import io
import math
import struct
import sys
import wave
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.assessment_operations import router as operations
from app.api.assessment_repetition import get_recording_store
from app.api.assessment_repetition import router as recordings
from app.api.assessment_session import router as assessments
from app.api.teacher_api import get_audio_store, get_db
from app.api.teacher_assessment import issue_access_token
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.qa_text_contract import valid_result
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    authorize_retry,
    claim_job,
    execute_claim,
    recording_input,
    schedule,
)
from app.media.audio_inspection import AudioError, AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.audio_normalization import prepare_audio as normalize
from app.media.fixed_audio import AudioData, AudioStore, prepare_audio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import add_entry, create_draft
from app.papers.publication import publish, select_plugin
from app.scoring.registry import EvaluationContract, Registry
from app.sessions.assessment_session import AssessmentSession, SessionTask, create_session
from app.sessions.submission import Submission
from app.taskbank.models import RevisionContent, TaskBank, append_revision

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCOPE = "local-qa-workbench-v1"
ASR = {"source": "local-mock-asr", "version": "1", "parameters": {}}
LLM = {
    "source": "local-mock-llm",
    "version": "1",
    "parameters": {},
    "depends_on": {"requirement_index": 0, "source": "local-mock-asr", "version": "1"},
}


class TestPlugin:
    plugin_id = "local-qa-workbench"
    implementation_version = "1"
    display_name = "本地测试合同（不计分）"
    selectable = True

    def __init__(self):
        self.evaluation_requirements = {"qa": [ASR, LLM]}

    def score_task(self, *args):
        raise RuntimeError("Local workbench does not score")

    def score_paper(self, *args):
        raise RuntimeError("Local workbench does not score")


REGISTRY = Registry(
    [TestPlugin()],
    [
        EvaluationContract(
            "local-mock-asr",
            "1",
            frozenset({"qa"}),
            lambda p: p == {},
            False,
            recognition_result=lambda p: {"status": p["status"], "text": p["text"]},
        ),
        EvaluationContract(
            "local-mock-llm",
            "1",
            frozenset({"qa"}),
            lambda p: p == {},
            False,
            accepts_no_text=False,
        ),
    ],
)


class Tone:
    def synthesize(self, source_text, config):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(
                b"".join(
                    struct.pack("<h", int(2500 * math.sin(2 * math.pi * 440 * i / 16000)))
                    for i in range(16000)
                )
            )
        return AudioData(output.getvalue(), "audio/wav")


class MockASR:
    def __init__(self, store, derived):
        self.store, self.derived = store, derived

    def resolve_inputs(self, db, submission, revision, requirement):
        recording = recording_input(db, submission)
        return InputSnapshot(
            {k: recording[k] for k in ("recording_id", "sha256", "media_type")},
            {"recording": recording},
        )

    def evaluate(self, inputs, requirement):
        tools = AudioTools()
        try:
            spec = AudioSpec(
                "wav",
                "pcm_s16le",
                16000,
                1,
                "local-mock-v1",
                tools.version(),
                tools.version(probe=True),
            )
            prepared = normalize(inputs.payload["recording"], spec, self.store, self.derived, tools)
        except AudioError as exc:
            raise KnownFailure(exc.code) from None
        except (ValueError, OSError):
            raise KnownFailure("AUDIO_UNAVAILABLE") from None
        return AdapterResult(
            {
                "status": "TEXT",
                "text": "小猫在休息。（Mock 固定文本）",
                "original_audio": asdict(prepared.original),
                "converted_audio": asdict(prepared.output),
            },
            provider="Mock",
            model="offline-mock-asr",
            engine_version="1",
        )

    def validate_result(self, result, requirement):
        return result.payload.get("status") == "TEXT"


class MockLLM:
    def resolve_inputs(self, db, submission, revision, requirement):
        return InputSnapshot({"revision_id": str(revision.task_revision_id)}, {})

    def evaluate(self, inputs, requirement):
        return AdapterResult(
            {
                "grammar_score": 0.75,
                "semantic_score": 1.5,
                "pragmatic_score": 0.5,
                "completeness_score": 0.5,
                "dimension_reasons": {
                    k: "Mock 固定结果，不是真实评价"
                    for k in ("grammar", "semantic", "pragmatic", "completeness")
                },
                "review_recommended": True,
                "review_reasons": ["离线模拟"],
            },
            provider="Mock",
            model="offline-mock-llm",
            engine_version="1",
        )

    def validate_result(self, result, requirement):
        return valid_result(result.payload)


def create_app(engine, data_root):
    fixed = AudioStore(data_root / "fixed")
    store = RecordingStore(data_root / "recordings")
    derived = RecordingStore(data_root / "derived")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def db_dependency():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_audio_store] = lambda: fixed
    app.dependency_overrides[get_recording_store] = lambda: store
    app.include_router(assessments)
    app.include_router(operations)
    app.include_router(recordings)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Restrict DNS rebinding and browser-initiated cross-origin writes as well as peers.
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if (
            not request.client
            or request.client.host != "127.0.0.1"
            or host not in ("127.0.0.1:8000", "127.0.0.1")
            or (origin is not None and origin != "http://" + host)
            or (request.method not in ("GET", "HEAD") and origin != "http://" + host)
        ):
            return JSONResponse({"detail": "Loopback same-origin requests only"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    def owner(db, sid):
        row = db.get(AssessmentSession, sid)
        if not row or row.scope != SCOPE:
            raise HTTPException(404, "Test session not found")
        return row

    def selected_task(db, sid, task_id):
        tasks = db.scalars(select(SessionTask).where(SessionTask.session_id == sid)).all()
        if task_id is None and len(tasks) == 1:
            return tasks[0]
        task = next((item for item in tasks if item.session_task_id == task_id), None)
        if task is None:
            raise HTTPException(404, "Test session task not found")
        return task

    @app.get("/dev/qa")
    def page():
        return FileResponse(HERE / "index.html")

    @app.get("/dev/qa/app.js")
    def script():
        return FileResponse(HERE / "app.js", media_type="application/javascript")

    @app.get("/dev/qa/sessions")
    def sessions():
        with Session(engine) as db:
            return [
                {"session_id": str(s.session_id), "status": s.status}
                for s in db.scalars(
                    select(AssessmentSession)
                    .where(AssessmentSession.scope == SCOPE)
                    .order_by(AssessmentSession.created_at.desc())
                )
            ]

    @app.post("/dev/qa/sessions")
    def create(count: int = Query(default=1, ge=1, le=2)):
        with Session(engine) as db, db.begin():
            bank = TaskBank(bank_id=uuid4(), name="本地 QA 测试题库")
            db.add(bank)
            db.flush()
            paper = create_draft(db, bank_id=bank.bank_id, name="本地 QA 测试卷")
            pid = paper.paper_id
            for index in range(count):
                task = append_revision(
                    db,
                    bank_id=bank.bank_id,
                    task_code=f"qa-{index + 1}",
                    display_order=index + 1,
                    content=RevisionContent(
                        grade_level="测试",
                        task_type="qa",
                        prompt_text=("小猫在做什么？" if index == 0 else "小狗在做什么？"),
                        material_description=(
                            "一只小猫趴在垫子上。" if index == 0 else "一只小狗正在跑。"
                        ),
                        reference_answer=("小猫在休息。" if index == 0 else "小狗在跑。"),
                        scoring_instruction="语义相近的合理回答均可接受。",
                    ),
                    images={},
                )
                add_entry(
                    db,
                    paper_id=pid,
                    expected_version=index,
                    task_id=task.task_id,
                    position=index + 1,
                )
            select_plugin(
                db,
                paper_id=pid,
                expected_version=count,
                plugin_id=TestPlugin.plugin_id,
                version="1",
                registry=REGISTRY,
            )
            publish(
                db,
                paper_id=pid,
                expected_version=count + 1,
                registry=REGISTRY,
                audio_config={
                    "provider": "local-mock",
                    "model": "tone",
                    "voice": "tone",
                    "version": "1",
                    "speed": 1,
                },
            )
        prepare_audio(engine, pid, fixed, Tone())
        with Session(engine) as db, db.begin():
            session = create_session(
                db,
                paper_id=pid,
                scope=SCOPE,
                create_request_id=str(uuid4()),
                review_policy={"version": "v1", "default_ratio": 0},
                store=fixed,
                registry=REGISTRY,
            )
            sid = session.session_id
        return {"session_id": str(sid)}

    @app.post("/dev/qa/sessions/{sid}/token")
    def token(sid: UUID):
        with Session(engine) as db:
            owner(db, sid)
            db.rollback()
            result = issue_access_token(sid, Response(), db=db)
            return {"token": result.token}

    @app.get("/dev/qa/sessions/{sid}/diagnostics")
    def diagnostics(sid: UUID, task_id: UUID | None = None):
        with Session(engine) as db:
            owner(db, sid)
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                return {"submission": None, "jobs": []}
            recording = db.get(Recording, UUID(sub.inputs["recording_id"]))
            jobs = db.scalars(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == sub.submission_id)
                .order_by(EvaluationJob.requirement_index)
            ).all()
            output = []
            for job in jobs:
                raw = db.scalar(
                    select(RawEvaluation).where(
                        RawEvaluation.job_id == job.job_id, RawEvaluation.status == "READY"
                    )
                )
                output.append(
                    {
                        "job_id": str(job.job_id),
                        "source": job.requirement["source"],
                        "status": job.status,
                        "error": job.error_code,
                        "input_refs": job.input_refs,
                        "evidence": None
                        if raw is None
                        else {
                            "id": str(raw.evaluation_id),
                            "provider": raw.provider,
                            "model": raw.model,
                            "payload": raw.payload,
                        },
                    }
                )
            return {
                "submission": {"submission_id": str(sub.submission_id), "status": "ACCEPTED"},
                "recording": {
                    "recording_id": str(recording.recording_id),
                    "status": recording.status,
                    "media_type": recording.media_type,
                },
                "jobs": output,
            }

    @app.post("/dev/qa/sessions/{sid}/run-offline")
    def run_offline(sid: UUID, task_id: UUID | None = None):
        with Session(engine) as db, db.begin():
            owner(db, sid)
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                raise HTTPException(409, "No current submission")
            submission_id = sub.submission_id
            schedule(db, submission_id=submission_id)
        adapters = {
            ("local-mock-asr", "1"): MockASR(store, derived),
            ("local-mock-llm", "1"): MockLLM(),
        }
        with Session(engine) as db:
            ids = list(
                db.scalars(
                    select(EvaluationJob.job_id)
                    .where(EvaluationJob.submission_id == submission_id)
                    .order_by(EvaluationJob.requirement_index)
                )
            )
        for job_id in ids:
            claim = claim_job(
                engine, job_id, registry=REGISTRY, adapters=adapters, lease=timedelta(minutes=5)
            )
            if claim:
                execute_claim(engine, claim)
        return {"mode": "offline-mock"}

    @app.post("/dev/qa/sessions/{sid}/jobs/{job_id}/retry")
    def retry(sid: UUID, job_id: UUID, body: dict, task_id: UUID | None = None):
        if set(body) != {"accept_unknown"} or type(body["accept_unknown"]) is not bool:
            raise HTTPException(422, "Explicit retry acknowledgment required")
        with Session(engine) as db, db.begin():
            owner(db, sid)
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            job = db.get(EvaluationJob, job_id)
            if not sub or not job or job.submission_id != sub.submission_id:
                raise HTTPException(404, "Current test job not found")
            try:
                authorize_retry(db, job_id, accept_unknown=body["accept_unknown"])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"status": "WAITING"}

    return app


def main():
    import uvicorn

    config = dotenv_values(ROOT / "backend" / ".env")
    url = make_url(config.get("TEST_DATABASE_URL") or "")
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "vnext_eval02e_test"
        or url.host not in ("localhost", "127.0.0.1", "::1")
    ):
        raise RuntimeError("TEST_DATABASE_URL must be the local vnext_eval02e_test database")
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as conn:
        if conn.scalar(text("select current_database()")) != "vnext_eval02e_test":
            raise RuntimeError("Unexpected database")
    if sys.argv[1:] == ["--migrate"]:
        from alembic import command
        from alembic.config import Config

        config = Config(str(ROOT / "backend" / "alembic.ini"))
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            conn.commit()
        return
    if sys.argv[1:]:
        raise SystemExit("Usage: workbench.py [--migrate]")
    data_root = ROOT / "uploads" / "local-qa-workbench"
    uvicorn.run(create_app(engine, data_root), host="127.0.0.1", port=8000, proxy_headers=False)


if __name__ == "__main__":
    main()
