"""Loopback-only, offline QA workbench. Never mounted by the production app."""

import importlib.util
import io
import ipaddress
import math
import os
import socket
import struct
import sys
import wave
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.assessment_operations import router as operations
from app.api.assessment_repetition import get_recording_store
from app.api.assessment_repetition import router as recordings
from app.api.assessment_session import router as assessments
from app.api.auth import router as authentication
from app.api.db import get_auth_db
from app.api.teacher_api import get_audio_store, get_db
from app.api.teacher_assessment import issue_access_token
from app.api.teacher_auth import require_admin
from app.api.teacher_llm_provider import router as llm_provider
from app.evaluation import iflytek_ise as ise
from app.evaluation import qa_formal_text_contract as formal_text
from app.evaluation.asr_contract import MODEL as ASR_MODEL
from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
from app.evaluation.asr_contract import VERSION as ASR_VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.models import EvaluationDiagnosticEvidence, EvaluationJob, RawEvaluation
from app.evaluation.qa_text_contract import (
    valid_result,
)
from app.evaluation.qa_text_providers import DynamicQAText
from app.evaluation.source_identity import recording_input
from app.evaluation.worker import (
    AdapterResult,
    InputSnapshot,
    KnownFailure,
    authorize_retry,
    claim_job,
    execute_claim,
    schedule,
)
from app.media.audio_inspection import AudioError, AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.audio_normalization import prepare_audio as normalize
from app.media.fixed_audio import AudioData, AudioStore, prepare_audio
from app.media.recording import Recording
from app.media.recording_store import RecordingStore
from app.papers.paper import EvaluationPlan, Paper, PaperEntry, add_entry, create_draft
from app.papers.publication import publish, select_plugin
from app.scoring.registry import QWEN_ASR_CONTRACT, EvaluationContract, Registry
from app.sessions.assessment_session import AssessmentSession, SessionTask, create_session
from app.sessions.submission import Submission
from app.taskbank.models import RevisionContent, TaskBank, TaskRevision, append_revision

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_spec = importlib.util.spec_from_file_location("qa_authorization", HERE / "authorization.py")
authorization = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(authorization)
SCOPE = "local-qa-workbench-v1"
LLM_SOURCE = formal_text.SOURCE
LLM_VERSION = formal_text.VERSION
optional_body = Body(default=None)
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


class CandidatePlugin(TestPlugin):
    plugin_id = "local-qa-qwen-candidate"
    display_name = "Qwen 候选评价（不计分）"

    def __init__(self, version, requirements, task_type="qa"):
        self.implementation_version = version
        self.evaluation_requirements = {task_type: requirements}


def candidate_registry(version, requirements, task_type="qa"):
    return Registry(
        [CandidatePlugin(version, requirements, task_type)],
        [
            QWEN_ASR_CONTRACT,
            EvaluationContract(
                LLM_SOURCE,
                LLM_VERSION,
                frozenset({"qa"}),
                formal_text.valid_parameters,
                lambda p: p["evaluation"]["review_recommended"],
                accepts_no_text=False,
            ),
            EvaluationContract(
                ise.SOURCE, ise.VERSION, frozenset({"repetition"}), ise.valid_parameters, False
            ),
            EvaluationContract(
                "local-mock-ise", "1", frozenset({"repetition"}), ise.valid_parameters, False
            ),
        ],
    )


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


class MockISE:
    def resolve_inputs(self, session, submission, revision, requirement):
        if revision.task_type != "repetition" or not ise.valid_parameters(
            requirement["parameters"]
        ):
            raise ValueError("Invalid mock Repetition input")
        recording = recording_input(session, submission)
        return InputSnapshot(
            refs={key: recording[key] for key in ("recording_id", "sha256", "media_type")},
            payload={"recording": recording},
        )

    def evaluate(self, inputs, requirement):
        return AdapterResult(payload={"simulated": True}, provider="Mock", model="offline-mock-ise")

    def validate_result(self, result, requirement):
        return result.provider == "Mock" and result.payload == {"simulated": True}


def create_app(
    engine,
    data_root,
    *,
    paid_enabled=False,
    client_factory=None,
    text_client_factories=None,
    ise_socket_factory=None,
):
    url = engine.url
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in ("localhost", "127.0.0.1", "::1")
        or not url.port
        or not url.database
        or not (url.database.startswith("test_") or url.database.endswith("_test"))
        or url.query
        or any(k.upper().startswith("PG") and v for k, v in os.environ.items())
    ):
        raise ValueError("Workbench requires an explicitly selected local test database")
    with engine.connect() as connection:
        database, address, port = connection.execute(
            text("SELECT current_database(), inet_server_addr(), inet_server_port()")
        ).one()
        if (
            database != url.database
            or not ipaddress.ip_address(str(address)).is_loopback
            or (url.host != "localhost" and str(address) != url.host)
            or port != url.port
        ):
            raise ValueError("Unexpected workbench test database connection")
    authorization.initialize(engine)
    fixed = AudioStore(data_root / "fixed")
    store = RecordingStore(data_root / "recordings")
    derived = RecordingStore(data_root / "derived")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def business_db_dependency():
        with Session(engine) as db:
            yield db

    def auth_db_dependency():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = business_db_dependency
    app.dependency_overrides[get_auth_db] = auth_db_dependency
    app.dependency_overrides[get_audio_store] = lambda: fixed
    app.dependency_overrides[get_recording_store] = lambda: store
    app.include_router(authentication)
    app.include_router(llm_provider)
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

    def candidate_paper(db, owner):
        paper = db.get(Paper, owner.paper_id)
        if paper.scoring_plugin_id == TestPlugin.plugin_id:
            return None
        plan = db.scalar(
            select(EvaluationPlan)
            .join(PaperEntry)
            .where(PaperEntry.paper_id == owner.paper_id)
            .order_by(PaperEntry.position)
            .limit(1)
        )
        if plan is None or paper.scoring_plugin_id != CandidatePlugin.plugin_id:
            raise HTTPException(409, "Historical workbench session must be recreated")
        sources = {item["source"] for item in plan.requirements}
        mode = (
            "ise"
            if ise.SOURCE in sources
            else "mock-ise"
            if "local-mock-ise" in sources
            else "qwen"
        )
        return {
            "mode": mode,
            "requirements": plan.requirements,
            "plugin_version": paper.scoring_plugin_version,
        }

    @app.get("/dev/qa")
    def page():
        return FileResponse(HERE / "index.html")

    @app.get("/dev/qa/app.js")
    def script():
        return FileResponse(HERE / "app.js", media_type="application/javascript")

    @app.get("/dev/qa/sessions", dependencies=[Depends(require_admin)])
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

    @app.post("/dev/qa/sessions", dependencies=[Depends(require_admin)])
    def create(
        count: int = Query(default=1, ge=1, le=2),
        mode: str = "mock",
        body: dict | None = optional_body,
    ):
        body = body or {}
        if set(body) - {"task_type", "prompt_text", "reference_answer", "group", "grade"}:
            raise HTTPException(422, "Invalid test task")
        task_type = body.get("task_type", "qa")
        if task_type not in ("qa", "repetition") or mode not in ("mock", "qwen", "ise"):
            raise HTTPException(422, "Unknown test mode")
        if (mode == "qwen" and task_type != "qa") or (mode == "ise" and task_type != "repetition"):
            raise HTTPException(422, "Mode does not match task type")
        prompt = body.get("prompt_text", "请跟读这句话。" if task_type == "repetition" else None)
        reference = body.get(
            "reference_answer", "今天天气真好。" if task_type == "repetition" else None
        )
        if any(
            value is not None and (not isinstance(value, str) or not value.strip())
            for value in (prompt, reference)
        ):
            raise HTTPException(422, "Invalid test task text")
        registry, plugin_id, plugin_version = REGISTRY, TestPlugin.plugin_id, "1"
        requirements = None
        if mode == "qwen" or task_type == "repetition":
            tools = AudioTools()
            spec = AudioSpec(
                "wav",
                "pcm_s16le",
                16000,
                1,
                "local-workbench-v2",
                tools.version(),
                tools.version(probe=True),
            )
            if task_type == "qa":
                region = os.environ.get("VNEXT_QA_ASR_REGION", "")
                workspace = os.environ.get("VNEXT_QA_ASR_WORKSPACE", "")
                requirements = [
                    {
                        "source": ASR_SOURCE,
                        "version": ASR_VERSION,
                        "parameters": {
                            "model": ASR_MODEL,
                            "region": region,
                            "workspace": workspace,
                            "enable_itn": False,
                            "language": None,
                            "audio_spec": asdict(spec),
                        },
                    },
                    {
                        "source": LLM_SOURCE,
                        "version": LLM_VERSION,
                        "parameters": {
                            "prompt_version": formal_text.PROMPT_VERSION,
                            "prompt_sha256": formal_text.PROMPT_SHA256,
                        },
                        "depends_on": {
                            "requirement_index": 0,
                            "source": ASR_SOURCE,
                            "version": ASR_VERSION,
                        },
                    },
                ]
                if not QWEN_ASR_CONTRACT.validate_parameters(requirements[0]["parameters"]):
                    raise HTTPException(422, "Configure ASR region and workspace")
            else:
                params = {
                    "category": "read_sentence",
                    "group": body.get("group", "adult"),
                    "grade": body.get("grade"),
                    "audio_spec": asdict(spec),
                }
                if not ise.valid_parameters(params):
                    raise HTTPException(422, "Invalid ISE request parameters")
                requirements = [
                    {
                        "source": ise.SOURCE if mode == "ise" else "local-mock-ise",
                        "version": "1",
                        "parameters": params,
                    }
                ]
            plugin_id, plugin_version = CandidatePlugin.plugin_id, str(uuid4())
            registry = candidate_registry(plugin_version, requirements, task_type)
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
                    task_code=f"{task_type}-{index + 1}",
                    display_order=index + 1,
                    content=RevisionContent(
                        grade_level="测试",
                        task_type=task_type,
                        prompt_text=prompt
                        or ("小猫在做什么？" if index == 0 else "小狗在做什么？"),
                        material_description=(
                            "一只小猫趴在垫子上。" if index == 0 else "一只小狗正在跑。"
                        ),
                        reference_answer=reference
                        or ("小猫在休息。" if index == 0 else "小狗在跑。"),
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
                plugin_id=plugin_id,
                version=plugin_version,
                registry=registry,
            )
            publish(
                db,
                paper_id=pid,
                expected_version=count + 1,
                registry=registry,
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
                registry=registry,
            )
            sid = session.session_id
        return {"session_id": str(sid)}

    @app.post("/dev/qa/sessions/{sid}/token", dependencies=[Depends(require_admin)])
    def token(sid: UUID):
        with Session(engine) as db:
            owner(db, sid)
            db.rollback()
            result = issue_access_token(sid, Response(), db=db)
            return {"token": result.token}

    @app.get("/dev/qa/sessions/{sid}/diagnostics", dependencies=[Depends(require_admin)])
    def diagnostics(sid: UUID, task_id: UUID | None = None):
        with Session(engine) as db:
            session_owner = owner(db, sid)
            candidate = candidate_paper(db, session_owner)
            task = selected_task(db, sid, task_id)
            entry = db.get(PaperEntry, task.paper_entry_id)
            revision = db.get(TaskRevision, entry.task_revision_id)
            plan = db.get(EvaluationPlan, task.paper_entry_id)
            ise_requirement = next(
                (
                    item
                    for item in plan.requirements
                    if item["source"] in (ise.SOURCE, "local-mock-ise")
                ),
                None,
            )
            parameters = ise_requirement["parameters"] if ise_requirement else {}
            frozen_task = {
                "task_type": revision.task_type,
                "prompt_text": revision.prompt_text,
                "reference_answer": revision.reference_answer,
                "ise_group": parameters.get("group"),
                "ise_grade": parameters.get("grade"),
            }
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                return {
                    "frozen_task": frozen_task,
                    "submission": None,
                    "jobs": [],
                    "mode": candidate["mode"] if candidate else "mock",
                    "requirements": candidate["requirements"] if candidate else None,
                    "authorization": None,
                }
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
                failures = db.scalars(
                    select(RawEvaluation)
                    .where(RawEvaluation.job_id == job.job_id, RawEvaluation.status != "READY")
                    .order_by(RawEvaluation.created_at)
                ).all()
                output.append(
                    {
                        "job_id": str(job.job_id),
                        "source": job.requirement["source"],
                        "status": job.status,
                        "attempts": job.attempts,
                        "error": job.error_code,
                        "input_refs": job.input_refs,
                        "execution_snapshot": job.execution_snapshot,
                        "diagnostics": [
                            {
                                "status": item.status,
                                "error": item.error_code,
                                "evidence": diagnostic.evidence
                                if (
                                    diagnostic := db.get(
                                        EvaluationDiagnosticEvidence, item.evaluation_id
                                    )
                                )
                                else None,
                            }
                            for item in failures
                        ],
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
            permissions = (
                db.execute(
                    select(authorization.authorizations).where(
                        authorization.authorizations.c.submission_id == str(sub.submission_id)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "frozen_task": frozen_task,
                "mode": candidate["mode"] if candidate else "mock",
                "requirements": candidate["requirements"] if candidate else None,
                "authorization": {item["source"]: item["available"] for item in permissions},
                "submission": {"submission_id": str(sub.submission_id), "status": "ACCEPTED"},
                "recording": {
                    "recording_id": str(recording.recording_id),
                    "status": recording.status,
                    "media_type": recording.media_type,
                },
                "jobs": output,
            }

    @app.post("/dev/qa/sessions/{sid}/run-offline", dependencies=[Depends(require_admin)])
    def run_offline(sid: UUID, task_id: UUID | None = None):
        with Session(engine) as db, db.begin():
            session_owner = owner(db, sid)
            candidate = candidate_paper(db, session_owner)
            if candidate and candidate["mode"] != "mock-ise":
                raise HTTPException(409, "Use the separately authorized candidate path")
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
            ("local-mock-ise", "1"): MockISE(),
        }
        with Session(engine) as db:
            ids = list(
                db.scalars(
                    select(EvaluationJob.job_id)
                    .where(EvaluationJob.submission_id == submission_id)
                    .order_by(EvaluationJob.requirement_index)
                )
            )
        registry = (
            candidate_registry(candidate["plugin_version"], candidate["requirements"], "repetition")
            if candidate
            else REGISTRY
        )
        for job_id in ids:
            claim = claim_job(
                engine, job_id, registry=registry, adapters=adapters, lease=timedelta(minutes=5)
            )
            if claim:
                execute_claim(engine, claim)
        return {"mode": "offline-mock"}

    @app.post("/dev/qa/sessions/{sid}/authorize-paid", dependencies=[Depends(require_admin)])
    def authorize_paid(sid: UUID, body: dict, task_id: UUID | None = None):
        if not paid_enabled:
            raise HTTPException(403, "Paid tests disabled in server configuration")
        if body != {"authorized_audio": True, "confirm_paid": True} or any(
            type(v) is not bool for v in body.values()
        ):
            raise HTTPException(422, "Explicit paid and audio authorization required")
        from sqlalchemy.dialects.postgresql import insert

        with Session(engine) as db, db.begin():
            candidate = candidate_paper(db, owner(db, sid))
            if not candidate or candidate["mode"] not in ("qwen", "ise"):
                raise HTTPException(409, "Not a paid test session")
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                raise HTTPException(409, "No current submission")
            sources = [ASR_SOURCE, LLM_SOURCE] if candidate["mode"] == "qwen" else [ise.SOURCE]
            for source in sources:
                stmt = insert(authorization.authorizations).values(
                    submission_id=str(sub.submission_id), source=source, available=True
                )
                db.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["submission_id", "source"],
                        set_={"available": True, "authorized_at": func.now()},
                    )
                )
        return {"status": "authorized", "sources": sources}

    @app.post("/dev/qa/sessions/{sid}/run-candidate", dependencies=[Depends(require_admin)])
    def run_candidate(sid: UUID, task_id: UUID | None = None):
        if not paid_enabled:
            raise HTTPException(403, "Paid tests disabled in server configuration")
        with Session(engine) as db, db.begin():
            candidate = candidate_paper(db, owner(db, sid))
            if not candidate or candidate["mode"] not in ("qwen", "ise"):
                raise HTTPException(409, "Not a paid test session")
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if sub is None:
                raise HTTPException(409, "No current submission")
            submission_id = sub.submission_id
            permission = db.scalar(
                select(authorization.authorizations.c.submission_id)
                .where(
                    authorization.authorizations.c.submission_id == str(submission_id),
                    authorization.authorizations.c.available.is_(True),
                )
                .limit(1)
            )
            if permission is None:
                raise HTTPException(403, "Explicit authorization required")
            schedule(db, submission_id=submission_id)
        tools = AudioTools()
        adapters = {
            (ASR_SOURCE, ASR_VERSION): DashScopeASR(
                store, derived, tools, client_factory=client_factory
            ),
            (LLM_SOURCE, LLM_VERSION): DynamicQAText(client_factories=text_client_factories),
            (ise.SOURCE, ise.VERSION): ise.IseRepetition(
                store, derived, tools, websocket_factory=ise_socket_factory
            ),
        }
        task_type = "repetition" if candidate["mode"] == "ise" else "qa"
        registry = candidate_registry(
            candidate["plugin_version"], candidate["requirements"], task_type
        )
        with Session(engine) as db:
            jobs = [
                (row.job_id, row.requirement["source"])
                for row in db.scalars(
                    select(EvaluationJob)
                    .where(EvaluationJob.submission_id == submission_id)
                    .order_by(EvaluationJob.requirement_index)
                ).all()
            ]
        for job_id, source in jobs:
            if source == LLM_SOURCE:
                with Session(engine) as db:
                    asr_status = db.scalar(
                        select(EvaluationJob.status).where(
                            EvaluationJob.submission_id == submission_id,
                            EvaluationJob.requirement["source"].astext == ASR_SOURCE,
                        )
                    )
                if asr_status != "READY":
                    continue
            with Session(engine) as db, db.begin():
                consumed = db.scalar(
                    update(authorization.authorizations)
                    .where(
                        authorization.authorizations.c.submission_id == str(submission_id),
                        authorization.authorizations.c.source == source,
                        authorization.authorizations.c.available.is_(True),
                    )
                    .values(available=False)
                    .returning(authorization.authorizations.c.submission_id)
                )
            if not consumed:
                continue
            claim = claim_job(
                engine,
                job_id,
                registry=registry,
                adapters=adapters,
                lease=timedelta(minutes=20),
                recording_store=store,
                derived_recording_store=derived,
            )
            if claim:
                execute_claim(engine, claim)
        return {"mode": candidate["mode"]}

    @app.get("/dev/qa/ise-status", dependencies=[Depends(require_admin)])
    def ise_status():
        keys = ("IFLYTEK_ISE_APP_ID", "IFLYTEK_ISE_API_KEY", "IFLYTEK_ISE_API_SECRET")
        configured = {key: bool(os.environ.get(key, "").strip()) for key in keys}
        with Session(engine) as db:
            latest = db.execute(
                select(RawEvaluation.status, RawEvaluation.error_code, RawEvaluation.created_at)
                .join(EvaluationJob, EvaluationJob.job_id == RawEvaluation.job_id)
                .join(Submission, Submission.submission_id == EvaluationJob.submission_id)
                .join(SessionTask, SessionTask.session_task_id == Submission.session_task_id)
                .join(AssessmentSession, AssessmentSession.session_id == SessionTask.session_id)
                .where(
                    AssessmentSession.scope == SCOPE,
                    EvaluationJob.requirement["source"].astext == ise.SOURCE,
                )
                .order_by(RawEvaluation.created_at.desc())
                .limit(1)
            ).first()
        return {
            "fields": configured,
            "configured": all(configured.values()),
            "last_call": {
                "status": latest.status,
                "error": latest.error_code,
                "at": latest.created_at,
            }
            if latest
            else None,
        }

    @app.post("/dev/qa/sessions/{sid}/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
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

    load_dotenv(ROOT / "backend" / ".env")
    url = make_url(os.environ.get("TEST_DATABASE_URL") or "")
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "vnext_eval02e_test"
        or url.host not in ("127.0.0.1", "::1")
        or url.port is None
        or bool(url.query)
        or any(k.upper().startswith("PG") and v for k, v in os.environ.items())
    ):
        raise RuntimeError("TEST_DATABASE_URL must be the local vnext_eval02e_test database")
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    with engine.connect() as conn:
        sock = socket.socket(fileno=conn.connection.driver_connection.fileno())
        try:
            peer = sock.getpeername()
        finally:
            sock.detach()
        if (
            conn.scalar(text("select current_database()")) != "vnext_eval02e_test"
            or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(url.host)
            or peer[1] != url.port
        ):
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
    uvicorn.run(
        create_app(
            engine,
            data_root,
            paid_enabled=os.environ.get("VNEXT_QA_PAID_TESTS") == "enabled",
        ),
        host="127.0.0.1",
        port=8000,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
