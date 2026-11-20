"""Loopback-only, offline QA workbench. Never mounted by the production app."""

import importlib.util
import io
import ipaddress
import math
import os
import socket
import struct
import subprocess
import sys
import wave
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.assessment_operations import router as operations
from app.api.assessment_repetition import get_recording_store
from app.api.assessment_repetition import router as recordings
from app.api.assessment_session import router as assessments
from app.api.teacher_api import get_audio_store, get_db
from app.api.teacher_assessment import issue_access_token
from app.evaluation.asr_contract import MODEL as ASR_MODEL
from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
from app.evaluation.asr_contract import VERSION as ASR_VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.qa_text_contract import (
    PROMPT_SHA256,
    PROMPT_VERSION,
    valid_parameters,
    valid_result,
)
from app.evaluation.qa_text_contract import SOURCE as LLM_SOURCE
from app.evaluation.qa_text_contract import VERSION as LLM_VERSION
from app.evaluation.qwen_qa_text import QwenQAText
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
from app.scoring.registry import QWEN_ASR_CONTRACT, EvaluationContract, Registry
from app.sessions.assessment_session import AssessmentSession, SessionTask, create_session
from app.sessions.submission import Submission
from app.taskbank.models import RevisionContent, TaskBank, append_revision

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_spec = importlib.util.spec_from_file_location("qa_provider_config", HERE / "provider_config.py")
provider_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provider_config)
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


class CandidatePlugin(TestPlugin):
    plugin_id = "local-qa-qwen-candidate"
    display_name = "Qwen 候选评价（不计分）"

    def __init__(self, version, requirements):
        self.implementation_version = version
        self.evaluation_requirements = {"qa": requirements}


def candidate_registry(version, requirements):
    return Registry(
        [CandidatePlugin(version, requirements)],
        [
            QWEN_ASR_CONTRACT,
            EvaluationContract(
                LLM_SOURCE,
                LLM_VERSION,
                frozenset({"qa"}),
                valid_parameters,
                lambda p: p["evaluation"]["review_recommended"],
                accepts_no_text=False,
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


def create_app(engine, data_root, *, paid_enabled=False, client_factory=None, master_key=None):
    if master_key is None:
        master_key = os.environ.get("VNEXT_PROVIDER_CREDENTIAL_KEY")
    provider_config.initialize(engine)
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

    def candidate_paper(db, owner):
        return (
            db.execute(
                select(provider_config.paper_configs).where(
                    provider_config.paper_configs.c.paper_id == str(owner.paper_id)
                )
            )
            .mappings()
            .one_or_none()
        )

    @app.get("/dev/qa")
    def page():
        return FileResponse(HERE / "index.html")

    @app.get("/dev/qa/app.js")
    def script():
        return FileResponse(HERE / "app.js", media_type="application/javascript")

    @app.get("/dev/qa/provider-config")
    def provider_status():
        with Session(engine) as db:
            return provider_config.status(db, master_key)

    @app.post("/dev/qa/provider-config")
    async def provider_save(request: Request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {
                "region",
                "workspace",
                "api_key",
                "retain_credential_id",
            }:
                raise ValueError
            with Session(engine) as db, db.begin():
                config_id = provider_config.save(
                    db,
                    master_key,
                    region=data["region"],
                    workspace=data["workspace"],
                    api_key=data["api_key"],
                    retain_credential_id=data["retain_credential_id"],
                )
            return {"config_id": config_id}
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Provider configuration unavailable or invalid") from None

    @app.post("/dev/qa/provider-config/{credential_id}/disable")
    def provider_disable(credential_id: UUID):
        try:
            with Session(engine) as db, db.begin():
                provider_config.disable(db, credential_id)
            return {"status": "disabled"}
        except provider_config.CredentialUnavailable:
            raise HTTPException(404, "Credential unavailable") from None

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
    def create(count: int = Query(default=1, ge=1, le=2), mode: str = "mock"):
        if mode not in ("mock", "qwen"):
            raise HTTPException(422, "Unknown test mode")
        registry, plugin_id, plugin_version = REGISTRY, TestPlugin.plugin_id, "1"
        requirements = None
        config_id = None
        if mode == "qwen":
            with Session(engine) as db:
                config = provider_config.status(db, master_key)
            if not config.get("configured"):
                raise HTTPException(409, "Provider configuration unavailable")
            config_id = config["config_id"]
            tools = AudioTools()
            spec = AudioSpec(
                "wav",
                "pcm_s16le",
                16000,
                1,
                "local-qa-v1",
                tools.version(),
                tools.version(probe=True),
            )
            requirements = [
                {
                    "source": ASR_SOURCE,
                    "version": ASR_VERSION,
                    "parameters": {
                        "model": ASR_MODEL,
                        "region": config["region"],
                        "workspace": config["workspace"],
                        "enable_itn": False,
                        "language": None,
                        "audio_spec": asdict(spec),
                    },
                },
                {
                    "source": LLM_SOURCE,
                    "version": LLM_VERSION,
                    "parameters": {
                        "provider": "dashscope",
                        "model": "qwen-plus",
                        "region": config["region"],
                        "workspace": config["workspace"],
                        "request_version": "dashscope-chat-json-v1",
                        "prompt_version": PROMPT_VERSION,
                        "prompt_sha256": PROMPT_SHA256,
                        "response_format": "json_object",
                        "temperature": 0.2,
                    },
                    "depends_on": {
                        "requirement_index": 0,
                        "source": ASR_SOURCE,
                        "version": ASR_VERSION,
                    },
                },
            ]
            plugin_id, plugin_version = CandidatePlugin.plugin_id, str(uuid4())
            registry = candidate_registry(plugin_version, requirements)
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
            if requirements is not None:
                db.execute(
                    provider_config.paper_configs.insert().values(
                        paper_id=str(pid),
                        config_id=config_id,
                        plugin_version=plugin_version,
                        requirements=requirements,
                    )
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
            session_owner = owner(db, sid)
            candidate = candidate_paper(db, session_owner)
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                return {
                    "submission": None,
                    "jobs": [],
                    "mode": "qwen" if candidate else "mock",
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
                output.append(
                    {
                        "job_id": str(job.job_id),
                        "source": job.requirement["source"],
                        "status": job.status,
                        "attempts": job.attempts,
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
            authorization = (
                db.execute(
                    select(provider_config.authorizations).where(
                        provider_config.authorizations.c.submission_id == str(sub.submission_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return {
                "mode": "qwen" if candidate else "mock",
                "requirements": candidate["requirements"] if candidate else None,
                "authorization": None
                if authorization is None
                else {
                    "asr_consumed": authorization["asr_consumed"],
                    "llm_consumed": authorization["llm_consumed"],
                    "authorized_at": authorization["authorized_at"],
                },
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
            session_owner = owner(db, sid)
            if candidate_paper(db, session_owner) is not None:
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

    @app.post("/dev/qa/sessions/{sid}/authorize-paid")
    def authorize_paid(sid: UUID, body: dict, task_id: UUID | None = None):
        if not paid_enabled:
            raise HTTPException(403, "Paid tests disabled in server configuration")
        if body != {"authorized_audio": True, "confirm_paid": True} or any(
            type(v) is not bool for v in body.values()
        ):
            raise HTTPException(422, "Explicit paid and audio authorization required")
        with Session(engine) as db, db.begin():
            session_owner = owner(db, sid)
            if candidate_paper(db, session_owner) is None:
                raise HTTPException(409, "Not a candidate session")
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if not sub:
                raise HTTPException(409, "No current submission")
            from sqlalchemy.dialects.postgresql import insert

            result = db.scalar(
                insert(provider_config.authorizations)
                .values(submission_id=str(sub.submission_id))
                .on_conflict_do_nothing()
                .returning(provider_config.authorizations.c.submission_id)
            )
            if result is None:
                raise HTTPException(409, "Submission already authorized")
        return {"status": "authorized", "asr_budget": 1, "llm_budget": 1}

    @app.post("/dev/qa/sessions/{sid}/run-candidate")
    def run_candidate(sid: UUID, task_id: UUID | None = None):
        if not paid_enabled:
            raise HTTPException(403, "Paid tests disabled in server configuration")
        with Session(engine) as db, db.begin():
            session_owner = owner(db, sid)
            candidate = candidate_paper(db, session_owner)
            if candidate is None:
                raise HTTPException(409, "Not a candidate session")
            task = selected_task(db, sid, task_id)
            sub = db.scalar(
                select(Submission).where(Submission.attempt_id == task.current_attempt_id)
            )
            if sub is None:
                raise HTTPException(409, "No current submission")
            submission_id = sub.submission_id
            permission = db.execute(
                select(provider_config.authorizations).where(
                    provider_config.authorizations.c.submission_id == str(submission_id)
                )
            ).one_or_none()
            if permission is None:
                raise HTTPException(403, "Explicit authorization required")
            config = (
                db.execute(
                    select(provider_config.configs).where(
                        provider_config.configs.c.id == candidate["config_id"]
                    )
                )
                .mappings()
                .one()
            )
            credential_id = config["credential_id"]
            requirements = candidate["requirements"]
            version = candidate["plugin_version"]
            schedule(db, submission_id=submission_id)

        def credential():
            with Session(engine) as db:
                return provider_config.resolve(db, master_key, credential_id, "dashscope")

        if client_factory is None:
            import dashscope
            import httpx

            def factory(**kwargs):
                return dashscope.Client(**kwargs, http_client=httpx.Client(trust_env=False))
        else:
            factory = client_factory
        tools = AudioTools()
        adapters = {
            (ASR_SOURCE, ASR_VERSION): DashScopeASR(
                store, derived, tools, client_factory=factory, credential_provider=credential
            ),
            (LLM_SOURCE, LLM_VERSION): QwenQAText(
                client_factory=factory, credential_provider=credential
            ),
        }
        registry = candidate_registry(version, requirements)
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
            if source not in (ASR_SOURCE, LLM_SOURCE):
                continue
            column = (
                provider_config.authorizations.c.asr_consumed
                if source == ASR_SOURCE
                else provider_config.authorizations.c.llm_consumed
            )
            with Session(engine) as db:
                if db.scalar(
                    select(column).where(
                        provider_config.authorizations.c.submission_id == str(submission_id)
                    )
                ):
                    continue
            claim = claim_job(
                engine, job_id, registry=registry, adapters=adapters, lease=timedelta(minutes=20)
            )
            if claim is None:
                continue
            with Session(engine) as db, db.begin():
                consumed = db.scalar(
                    update(provider_config.authorizations)
                    .where(
                        provider_config.authorizations.c.submission_id == str(submission_id),
                        column.is_(False),
                    )
                    .values({column.name: True})
                    .returning(provider_config.authorizations.c.submission_id)
                )
            if consumed:
                execute_claim(engine, claim)
        return {"mode": "qwen-candidate"}

    @app.post("/dev/qa/sessions/{sid}/jobs/{job_id}/retry")
    def retry(sid: UUID, job_id: UUID, body: dict, task_id: UUID | None = None):
        if set(body) != {"accept_unknown"} or type(body["accept_unknown"]) is not bool:
            raise HTTPException(422, "Explicit retry acknowledgment required")
        with Session(engine) as db, db.begin():
            session_owner = owner(db, sid)
            if candidate_paper(db, session_owner) is not None:
                raise HTTPException(409, "Candidate test budgets do not permit retry")
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
    key_dir = Path.home() / ".codex" / "vnext-qa-private"
    key_path = key_dir / "provider.key"
    if sys.argv[1:] == ["--generate-provider-key"]:
        if (
            key_path.exists()
            or config.get("VNEXT_PROVIDER_CREDENTIAL_KEY")
            or os.environ.get("VNEXT_PROVIDER_CREDENTIAL_KEY")
        ):
            raise SystemExit("Provider key already exists; refusing to replace it")
        from cryptography.fernet import Fernet

        if key_dir.is_symlink():
            raise SystemExit("Unsafe private key directory")
        key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            account = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
            if account == "\\":
                raise SystemExit("Cannot establish private key permissions")
            subprocess.run(
                [
                    "icacls",
                    str(key_dir),
                    "/inheritance:r",
                    "/grant:r",
                    account + ":(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            key_dir.chmod(0o700)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as private_file:
            private_file.write(Fernet.generate_key())
        print("Provider master key saved in private configuration")
        return
    url = make_url(config.get("TEST_DATABASE_URL") or "")
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
        raise SystemExit("Usage: workbench.py [--migrate|--generate-provider-key]")
    data_root = ROOT / "uploads" / "local-qa-workbench"
    master_key = os.environ.get("VNEXT_PROVIDER_CREDENTIAL_KEY")
    if master_key is None and key_path.is_file() and not key_path.is_symlink():
        master_key = key_path.read_text(encoding="ascii").strip()
    uvicorn.run(
        create_app(
            engine,
            data_root,
            paid_enabled=os.environ.get("VNEXT_QA_PAID_TESTS") == "enabled",
            master_key=master_key,
        ),
        host="127.0.0.1",
        port=8000,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
