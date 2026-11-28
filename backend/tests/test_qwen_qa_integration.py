"""Persisted QA ASR to candidate Qwen text evaluation with offline SDK transports."""

import json
from dataclasses import asdict
from datetime import timedelta

import dashscope
import httpx
import pytest
import test_asr_dependency as common
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_asr_dependency import ACOUSTIC, Plugin, adapters, claim
from test_dashscope_asr import body as asr_body
from test_qwen_qa_text import output, parameters, response

from app.evaluation.asr_contract import MODEL as ASR_MODEL
from app.evaluation.asr_contract import SOURCE as ASR_SOURCE
from app.evaluation.asr_contract import VERSION as ASR_VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.identity import InputIdentityConflict
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.qa_text_contract import SOURCE, VERSION, valid_parameters
from app.evaluation.qwen_qa_text import QwenQAText
from app.evaluation.worker import (
    authorize_retry,
    claim_job,
    execute_claim,
    recover_expired,
    run_once,
)
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.models import FinalScoreResult
from app.scoring.registry import (
    PRODUCTION_REGISTRY,
    QWEN_ASR_CONTRACT,
    EvaluationContract,
    Registry,
)
from app.scoring.worker import run_settlement_once

pytest_plugins = ("test_asr_dependency",)


@pytest.fixture
def pipeline(dependency_flow, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "test-v1", tools.version(), tools.version(probe=True)
    )
    asr = {
        "source": ASR_SOURCE,
        "version": ASR_VERSION,
        "parameters": {
            "model": ASR_MODEL,
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "enable_itn": False,
            "language": None,
            "audio_spec": asdict(spec),
        },
    }
    llm = {
        "source": SOURCE,
        "version": VERSION,
        "parameters": parameters(),
        "depends_on": {"requirement_index": 2, "source": ASR_SOURCE, "version": ASR_VERSION},
    }
    plan = [llm, ACOUSTIC, asr]

    def registry_factory(requirements, allow_no_text=False, version2=False):
        plugin = Plugin(requirements)
        contracts = [
            QWEN_ASR_CONTRACT,
            EvaluationContract(
                SOURCE,
                VERSION,
                frozenset({"qa"}),
                valid_parameters,
                lambda p: p["evaluation"]["review_recommended"],
                accepts_no_text=False,
            ),
            EvaluationContract("test-acoustic", "1", frozenset({"qa"}), lambda p: p == {}, False),
        ]
        return plugin, Registry([plugin], contracts)

    monkeypatch.setattr(common, "make_registry", registry_factory)

    def create(close=True):
        f = dependency_flow(requirements=plan, close=close)
        calls = {"asr": [], "llm": []}
        responses = {"asr": asr_body("真实上游转写"), "llm": response(), "status": 200}

        def handler(request):
            kind = "asr" if "multimodal-generation" in request.url.path else "llm"
            calls[kind].append(json.loads(request.content))
            hook = responses.get("hook")
            if hook:
                hook(kind, len(calls[kind]))
            return httpx.Response(
                responses["status"] if kind == "llm" else responses.get("asr_status", 200),
                json=responses[kind],
            )

        def factory(**kwargs):
            return dashscope.Client(
                **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
            )

        asr_adapter = DashScopeASR(
            f["store"], RecordingStore(tmp_path / "derived"), tools, client_factory=factory
        )
        text_adapter = QwenQAText(client_factory=factory)
        _, _, acoustic, sources = adapters(f)
        sources[(ASR_SOURCE, ASR_VERSION)] = asr_adapter
        sources[(SOURCE, VERSION)] = text_adapter
        return f, sources, calls, responses, acoustic

    return create


def test_end_to_end_and_restart(db, pipeline):
    f, sources, calls, _, acoustic = pipeline()
    assert claim(db, f, SOURCE, sources) is None
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    assert execute_claim(db, claim(db, f, "test-acoustic", sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert raw.status == "READY" and raw.payload["evaluation"] == output()
        assert raw.input_refs["dependencies"]["asr"]["evaluation_id"]
        assert raw.input_refs["dependencies"]["asr"]["recording_id"] == f["recording_id"]
        assert raw.model == "qwen-plus" and raw.provider == "dashscope"
        assert {j.requirement["source"] for j in s.scalars(select(EvaluationJob))} == {
            SOURCE,
            ASR_SOURCE,
            "test-acoustic",
        }
    user = json.loads(calls["llm"][0]["input"]["messages"][1]["content"])
    assert user["asr_text"] == "真实上游转写"
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    db.dispose()
    run_once(
        db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
    )
    run_settlement_once(db, registry=f["registry"])
    assert len(calls["asr"]) == len(calls["llm"]) == acoustic.calls == 1
    with Session(db) as s:
        assert len(list(s.scalars(select(FinalScoreResult)))) == 1
    assert PRODUCTION_REGISTRY.resolve("test-only", "1") is None


def test_invalid_and_known_failure_retry_without_repeating_asr(db, pipeline):
    f, sources, calls, replies, _ = pipeline()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    replies["llm"] = response("```json\n{}\n```")
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    with Session(db) as s, s.begin():
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert job.status == "INVALID"
        authorize_retry(s, job.job_id)
    replies.update(status=401, llm={"code": "InvalidApiKey"})
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    with Session(db) as s, s.begin():
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert (job.status, job.error_code) == ("FAILED", "LLM_AUTHENTICATION")
        authorize_retry(s, job.job_id)
    replies.update(status=200, llm=response())
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    assert len(calls["asr"]) == 1 and len(calls["llm"]) == 3


def test_no_text_diagnostic_and_no_llm_call(db, pipeline):
    f, sources, calls, replies, _ = pipeline()
    replies["asr"] = asr_body("")
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert claim(db, f, SOURCE, sources) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert (job.status, job.error_code) == ("WAITING", "ASR_NO_TEXT_NOT_ACCEPTED")
    assert calls["llm"] == []


def test_changed_prompt_context_cannot_reclaim(db, pipeline):
    f, sources, calls, _, _ = pipeline()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    first = claim(db, f, SOURCE, sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        refs, digest = job.input_refs, job.input_fingerprint
    with db.begin() as connection:
        connection.execute(
            text(
                "UPDATE evaluation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:id"
            ),
            {"id": first.job_id},
        )
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    adapter = sources[(SOURCE, VERSION)]
    resolve = adapter.resolve_inputs

    def changed(*args):
        inputs = resolve(*args)
        inputs.payload["system_prompt"] += " changed"
        return inputs

    adapter.resolve_inputs = changed
    with pytest.raises(InputIdentityConflict):
        claim_job(
            db, first.job_id, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1)
        )
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        assert (job.input_refs, job.input_fingerprint) == (refs, digest)
    assert calls["llm"] == []


def test_asr_failure_blocks_text_call(db, pipeline):
    f, sources, calls, replies, _ = pipeline()
    replies.update(asr_status=401, asr={"code": "InvalidApiKey"})
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    assert claim(db, f, SOURCE, sources) is None
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"][ASR_SOURCE]).status == "FAILED"
        assert s.get(EvaluationJob, f["jobs"][SOURCE]).status == "WAITING"
    assert len(calls["asr"]) == 1 and not calls["llm"]


def test_llm_unknown_does_not_replay(db, pipeline):
    f, sources, calls, replies, _ = pipeline()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    replies.update(status=503, llm={"code": "InternalError"})
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    for _ in range(2):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"][SOURCE]).status == "UNKNOWN"
    assert len(calls["llm"]) == len(calls["asr"]) == 1


def test_old_llm_result_cannot_change_new_final_result(db, client, pipeline):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import UUID

    from test_qa_http import deliver, record, submit, upload

    from app.evaluation.worker import schedule

    f, sources, calls, replies, _ = pipeline(close=False)
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    old = claim(db, f, SOURCE, sources)
    entered, release = Event(), Event()

    def hook(kind, number):
        if kind == "llm" and number == 1:
            entered.set()
            assert release.wait(15)

    replies["hook"] = hook
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, old)
        try:
            assert entered.wait(10)
            task_url = f"{f['root']}/tasks/{f['tasks'][0]['session_task_id']}"
            assert (
                client.post(
                    task_url + "/redo",
                    headers=f["headers"],
                    json={"redo_request_id": "redo", "expected_attempt_id": f["attempt_id"]},
                ).status_code
                == 200
            )
            aid = deliver(client, f)
            rid = record(client, f, aid, req="new").json()["recording_id"]
            assert upload(client, f, aid, rid).status_code == 200
            sid = submit(client, f, aid, rid, request="new").json()["submission_id"]
            with Session(db) as s, s.begin():
                assert schedule(s) == 3
            with Session(db) as s:
                jobs = {
                    j.requirement["source"]: j.job_id
                    for j in s.scalars(
                        select(EvaluationJob).where(EvaluationJob.submission_id == UUID(sid))
                    )
                }
            fresh = {**f, "jobs": jobs}
            assert execute_claim(db, claim(db, fresh, ASR_SOURCE, sources))
            assert execute_claim(db, claim(db, fresh, SOURCE, sources))
            assert execute_claim(db, claim(db, fresh, "test-acoustic", sources))
            assert (
                client.post(
                    f["root"] + "/close", headers=f["headers"], json={"close_request_id": "close"}
                ).status_code
                == 200
            )
            assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
        finally:
            release.set()
        future.result(timeout=15)
    with Session(db) as s:
        final = list(s.scalars(select(FinalScoreResult)))
        raw = s.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == jobs[SOURCE], RawEvaluation.status == "READY"
            )
        )
        assert len(final) == 1 and str(raw.submission_id) == sid
        assert raw.input_refs["dependencies"]["asr"]["recording_id"] == rid
    assert len(calls["llm"]) == 2


def test_missing_prompt_version_is_diagnosed(db, pipeline, monkeypatch):
    import app.evaluation.qa_text_contract as contract

    f, sources, calls, _, _ = pipeline()
    assert execute_claim(db, claim(db, f, ASR_SOURCE, sources))
    monkeypatch.setattr(contract, "PROMPT_SHA256", "missing-deployed-version")
    assert claim(db, f, SOURCE, sources) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert (job.status, job.error_code) == ("WAITING", "EVALUATION_CONTRACT_UNAVAILABLE")
    assert not calls["llm"]
