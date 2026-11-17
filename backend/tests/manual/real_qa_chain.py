"""Opt-in paid EVAL-02E chain; not collected by the default pytest patterns."""

import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import dashscope
import httpx
import pytest
import test_asr_dependency as common
import test_qa_http
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from test_asr_dependency import Plugin, claim
from test_qwen_qa_text import parameters as text_parameters

from app.evaluation.asr_contract import MODEL as ASR_MODEL
from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
from app.evaluation.asr_contract import VERSION as ASR_VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.qa_text_contract import SOURCE, VERSION, valid_parameters
from app.evaluation.qwen_qa_text import QwenQAText
from app.evaluation.worker import execute_claim
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.models import FinalScoreResult
from app.scoring.registry import QWEN_ASR_CONTRACT, EvaluationContract, Registry

pytest_plugins = ("test_asr_dependency",)


@pytest.fixture
def guarded_flow(request, monkeypatch, tmp_path):
    if os.environ.get("EVAL02E_PAID_CALLS") != "ASR1_LLM1":
        pytest.skip("Explicit authorization for one ASR and one LLM call is required")
    if os.environ.get("EVAL02E_NON_CHILD_AUDIO") != "confirmed":
        pytest.fail("Non-child audio provenance must be explicitly confirmed", pytrace=False)
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        pytest.fail("Private DashScope key is not configured", pytrace=False)
    region = os.environ.get("EVAL02E_REGION", "")
    workspace = os.environ.get("EVAL02E_WORKSPACE", "")
    if region not in ("cn-beijing", "ap-southeast-1", "us-east-1") or not workspace:
        pytest.fail("Region and workspace must be configured", pytrace=False)
    try:
        url = make_url(os.environ.get("TEST_DATABASE_URL", ""))
        if (
            url.host not in ("localhost", "127.0.0.1", "::1")
            or url.database != "vnext_eval02e_test"
        ):
            raise ValueError
    except Exception:  # noqa: BLE001 - do not reveal connection strings
        pytest.fail("Use only the disposable local vnext_eval02e_test database", pytrace=False)
    audio_path = os.environ.get("EVAL02E_AUDIO_FILE")
    if not audio_path:
        pytest.fail("A separately authorized non-child audio file is required", pytrace=False)
    try:
        audio = Path(audio_path).read_bytes()
    except OSError:
        pytest.fail("Cannot read the authorized audio file", pytrace=False)
    if not audio:
        pytest.fail("The authorized audio file is empty", pytrace=False)
    review_file = os.environ.get("EVAL02E_PRIVATE_REVIEW_FILE")
    if review_file:
        destination = Path(review_file)
        repository = Path(__file__).resolve().parents[3]
        if (
            not destination.is_absolute()
            or destination.resolve().is_relative_to(repository)
            or destination.exists()
            or not destination.parent.is_dir()
        ):
            pytest.fail("Review file must be new and outside the repository", pytrace=False)

    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "eval02e-v1", tools.version(), tools.version(probe=True)
    )
    asr = {
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
    }
    params = {**text_parameters(), "region": region, "workspace": workspace}
    llm = {
        "source": SOURCE,
        "version": VERSION,
        "parameters": params,
        "depends_on": {"requirement_index": 1, "source": ASR_SOURCE, "version": ASR_VERSION},
    }
    plan = [llm, asr]

    def registry_factory(requirements, allow_no_text=False, version2=False):
        plugin = Plugin(requirements)
        contract = EvaluationContract(
            SOURCE,
            VERSION,
            frozenset({"qa"}),
            valid_parameters,
            lambda p: p["evaluation"]["review_recommended"],
            accepts_no_text=False,
        )
        return plugin, Registry([plugin], [QWEN_ASR_CONTRACT, contract])

    monkeypatch.setattr(common, "make_registry", registry_factory)
    monkeypatch.setattr(test_qa_http, "wav", lambda: audio)
    write_task = common.write

    def write_fictional_task(db, bank, content, **kwargs):
        content = replace(
            content,
            grade_level="middle",
            prompt_text="小猫在做什么？",
            material_description="一只小猫趴在垫子上。",
            reference_answer="小猫在休息。",
            scoring_instruction="语义相近的简短回答均可接受。",
        )
        return write_task(db, bank, content, **kwargs)

    monkeypatch.setattr(common, "write", write_fictional_task)
    dependency_flow = request.getfixturevalue("dependency_flow")
    db = request.getfixturevalue("db")
    f = dependency_flow(requirements=plan, close=False)
    counts = {"asr": 0, "llm": 0}

    def before_request(request):
        paths = {
            "/api/v1/services/aigc/multimodal-generation/generation": "asr",
            "/api/v1/services/aigc/text-generation/generation": "llm",
        }
        if (
            request.url.scheme != "https"
            or request.url.host != f"{workspace}.{region}.maas.aliyuncs.com"
            or request.url.path not in paths
        ):
            raise httpx.RequestError("Unexpected provider endpoint")
        kind = paths[request.url.path]
        counts[kind] += 1
        if counts[kind] > 1:
            raise httpx.RequestError("Single-call limit reached")

    def factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(event_hooks={"request": [before_request]})
        )

    sources = {
        (ASR_SOURCE, ASR_VERSION): DashScopeASR(
            f["store"], RecordingStore(tmp_path / "derived"), tools, client_factory=factory
        ),
        (SOURCE, VERSION): QwenQAText(client_factory=factory),
    }
    return db, f, sources, counts


def test_authorized_provider_chain(guarded_flow):
    db, f, sources, counts = guarded_flow
    summary = {
        "asr_calls": 0,
        "llm_calls": 0,
        "asr_status": "NOT_STARTED",
        "llm_status": "NOT_STARTED",
        "linked": False,
        "formal_result": False,
    }
    try:
        if claim(db, f, SOURCE, sources) is not None:
            pytest.fail("LLM did not wait for ASR", pytrace=False)
        work = claim(db, f, ASR_SOURCE, sources)
        if work is None or not execute_claim(db, work):
            pytest.fail("ASR job could not complete", pytrace=False)
        with Session(db) as s:
            job = s.get(EvaluationJob, f["jobs"][ASR_SOURCE])
            summary["asr_job_status"] = job.status
            summary["asr_error"] = job.error_code
            raw = s.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == job.job_id, RawEvaluation.status == "READY"
                )
            )
            if raw is None:
                pytest.fail("ASR did not produce READY evidence", pytrace=False)
            summary.update(
                asr_status=raw.payload["status"],
                asr_request_id=raw.payload.get("request_id"),
                asr_model=raw.model,
                asr_usage=raw.payload.get("usage", {}),
            )
            recognized_text = raw.payload["text"]
            asr_evaluation_id = str(raw.evaluation_id)
            if (
                str(raw.submission_id) != f["submission_id"]
                or raw.payload["audio_evidence"]["recording_id"] != f["recording_id"]
            ):
                pytest.fail("ASR input identity mismatch", pytrace=False)
            summary["formal_result"] = s.scalar(select(FinalScoreResult)) is not None
            if summary["formal_result"]:
                pytest.fail("Unexpected formal result", pytrace=False)
        if summary["asr_status"] == "NO_TEXT":
            if claim(db, f, SOURCE, sources) is not None:
                pytest.fail("NO_TEXT unexpectedly dispatched LLM", pytrace=False)
            summary["llm_status"] = "WAITING_NO_TEXT"
            return
        work = claim(db, f, SOURCE, sources)
        if work is None or not execute_claim(db, work):
            pytest.fail("LLM job could not complete", pytrace=False)
        with Session(db) as s:
            job = s.get(EvaluationJob, f["jobs"][SOURCE])
            summary["llm_status"], summary["llm_error"] = job.status, job.error_code
            raw = s.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == job.job_id, RawEvaluation.status == "READY"
                )
            )
            if raw is None:
                pytest.fail("LLM did not produce valid READY evidence", pytrace=False)
            source = raw.input_refs["dependencies"]["asr"]
            summary["linked"] = (
                source["evaluation_id"] == asr_evaluation_id
                and source["recording_id"] == f["recording_id"]
                and str(raw.submission_id) == f["submission_id"]
            )
            summary.update(
                llm_model=raw.model,
                llm_request_id=raw.payload.get("request_id"),
                llm_usage=raw.payload.get("usage", {}),
            )
            if not summary["linked"]:
                pytest.fail("ASR to LLM evidence identity mismatch", pytrace=False)
            summary["formal_result"] = s.scalar(select(FinalScoreResult)) is not None
            if summary["formal_result"]:
                pytest.fail("Unexpected formal result", pytrace=False)
            review_file = os.environ.get("EVAL02E_PRIVATE_REVIEW_FILE")
            if review_file:
                destination = Path(review_file)
                try:
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as file:
                        json.dump(
                            {
                                "fictional_question": "小猫在做什么？",
                                "fictional_context": "一只小猫趴在垫子上。",
                                "fictional_reference": "小猫在休息。",
                                "asr_text": recognized_text,
                                "evaluation": raw.payload["evaluation"],
                            },
                            file,
                            ensure_ascii=False,
                        )
                except OSError:
                    pytest.fail(
                        "Could not create private review file; do not retry automatically",
                        pytrace=False,
                    )
                summary["private_review_created"] = True
    finally:
        summary["asr_calls"], summary["llm_calls"] = counts["asr"], counts["llm"]
        print("EVAL02E " + json.dumps(summary, ensure_ascii=True, sort_keys=True))
