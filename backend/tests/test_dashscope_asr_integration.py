"""PostgreSQL worker integration with the production ASR adapter and offline transport."""

import json
from dataclasses import asdict
from datetime import timedelta

import dashscope
import httpx
import pytest
import test_asr_dependency as dependency_tests
import test_qa_http
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_asr_dependency import ACOUSTIC, LLM, Plugin, adapters, claim
from test_audio_preparation import make_wav
from test_dashscope_asr import body

from app.evaluation.asr_contract import MODEL, SOURCE, VERSION
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.worker import (
    InputIdentityConflict,
    claim_job,
    execute_claim,
    recover_expired,
    run_once,
)
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.models import FinalScoreResult
from app.scoring.registry import QWEN_ASR_CONTRACT, EvaluationContract, Registry
from app.scoring.worker import run_settlement_once

pytest_plugins = ("test_asr_dependency",)


@pytest.fixture
def flow(dependency_flow, monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    monkeypatch.setattr(test_qa_http, "wav", lambda: make_wav(rate=8000))
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "test-v1", tools.version(), tools.version(probe=True)
    )
    parameters = {
        "model": MODEL,
        "region": "cn-beijing",
        "workspace": "test-workspace",
        "enable_itn": True,
        "language": "zh",
        "audio_spec": asdict(spec),
    }
    requirement = {"source": SOURCE, "version": VERSION, "parameters": parameters}

    def make(allowed=False, close=True):
        llm = {
            **LLM,
            "depends_on": {"requirement_index": 2, "source": SOURCE, "version": VERSION},
            "version": "2" if allowed else "1",
        }
        plan = [llm, ACOUSTIC, requirement]

        def registry_factory(requirements, allow_no_text=False, version2=False):
            plugin = Plugin(requirements)
            contracts = [
                QWEN_ASR_CONTRACT,
                EvaluationContract(
                    "test-llm",
                    "2" if allowed else "1",
                    frozenset({"qa"}),
                    lambda p: p == {},
                    False,
                    accepts_no_text=allowed,
                ),
                EvaluationContract(
                    "test-acoustic", "1", frozenset({"qa"}), lambda p: p == {}, False
                ),
            ]
            return plugin, Registry([plugin], contracts)

        monkeypatch.setattr(dependency_tests, "make_registry", registry_factory)
        f = dependency_flow(requirements=plan, close=close)
        calls = []
        response = {"value": body("测试文本")}

        def handler(request):
            calls.append(json.loads(request.content))
            if response.get("hook"):
                response["hook"](len(calls))
            return httpx.Response(response.get("status", 200), json=response["value"])

        def factory(**kwargs):
            return dashscope.Client(
                **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
            )

        adapter = DashScopeASR(
            f["store"], RecordingStore(tmp_path / "derived"), tools, client_factory=factory
        )
        _, llm_adapter, acoustic, sources = adapters(f)
        sources[(SOURCE, VERSION)] = adapter
        return f, adapter, llm_adapter, acoustic, sources, calls, response

    return make


@pytest.mark.parametrize("no_text", [False, True])
def test_persistent_asr_to_llm_and_restart(db, flow, no_text):
    f, _, llm, acoustic, sources, calls, response = flow(allowed=no_text)
    if no_text:
        response["value"] = body("")
    assert claim(db, f, "test-llm", sources) is None
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert str(raw.submission_id) == f["submission_id"]
        assert raw.provider == "dashscope" and raw.model == MODEL
        assert raw.payload["audio_evidence"]["recording_id"] == f["recording_id"]
        assert raw.payload["audio_evidence"]["reused_original"] is False
        assert raw.payload["audio_evidence"]["output"]["sample_rate"] == 16000
        assert (
            raw.payload["audio_evidence"]["original_sha256"]
            == job.input_refs["dependencies"]["sha256"]
        )
        assert raw.payload["status"] == ("NO_TEXT" if no_text else "TEXT")
        assert "confidence" not in raw.payload
    assert execute_claim(db, claim(db, f, "test-llm", sources))
    assert execute_claim(db, claim(db, f, "test-acoustic", sources))
    assert llm.received[0].payload["asr"] == {
        "status": "NO_TEXT" if no_text else "TEXT",
        "text": None if no_text else "测试文本",
    }
    assert len(run_settlement_once(db, registry=f["registry"])["completed"]) == 1
    db.dispose()
    for _ in range(2):
        assert (
            run_once(
                db,
                registry=f["registry"],
                adapters=sources,
                lease=timedelta(minutes=1),
                batch_size=10,
            )["claimed"]
            == 0
        )
        run_settlement_once(db, registry=f["registry"])
    with Session(db) as s:
        assert len(list(s.scalars(select(RawEvaluation)))) == 3
        assert len(list(s.scalars(select(FinalScoreResult)))) == 1
    assert len(calls) == 1 and acoustic.calls == 1


def test_provider_failure_blocks_llm_but_not_acoustic(db, flow):
    f, _, llm, acoustic, sources, calls, response = flow()
    response.update(status=401, value={"code": "InvalidApiKey"})
    for _ in range(2):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    with Session(db) as s:
        asr = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert (asr.status, asr.error_code) == ("FAILED", "ASR_AUTHENTICATION")
        assert s.get(EvaluationJob, f["jobs"]["test-llm"]).status == "WAITING"
        assert s.get(EvaluationJob, f["jobs"]["test-acoustic"]).status == "READY"
    assert len(calls) == 1 and llm.calls == 0 and acoustic.calls == 1


def test_frozen_contract_detects_changed_recording(db, flow):
    f, _, _, _, sources, calls, _ = flow()
    first = claim(db, f, SOURCE, sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        refs, fingerprint = job.input_refs, job.input_fingerprint
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
        snapshot = resolve(*args)
        snapshot.refs["parameters"] = {**snapshot.refs["parameters"], "enable_itn": False}
        return snapshot

    adapter.resolve_inputs = changed
    with pytest.raises(InputIdentityConflict):
        claim_job(
            db, first.job_id, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1)
        )
    with Session(db) as s:
        job = s.get(EvaluationJob, first.job_id)
        assert (job.input_refs, job.input_fingerprint) == (refs, fingerprint)
    assert calls == []


def test_late_asr_after_redo_cannot_contaminate_result(db, client, flow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import UUID

    from test_qa_http import deliver, record, submit, upload

    from app.evaluation.worker import schedule

    f, _, llm, _, sources, calls, response = flow(close=False)
    old = claim(db, f, SOURCE, sources)
    entered, release = Event(), Event()

    def hold_first(number):
        if number == 1:
            entered.set()
            assert release.wait(15)

    response["hook"] = hold_first
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_claim, db, old)
        try:
            assert entered.wait(10)
            task_url = f"{f['root']}/tasks/{f['tasks'][0]['session_task_id']}"
            assert (
                client.post(
                    task_url + "/redo",
                    headers=f["headers"],
                    json={"redo_request_id": "new", "expected_attempt_id": f["attempt_id"]},
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
                    job.requirement["source"]: job.job_id
                    for job in s.scalars(
                        select(EvaluationJob).where(EvaluationJob.submission_id == UUID(sid))
                    )
                }
            fresh = {**f, "jobs": jobs}
            assert execute_claim(db, claim(db, fresh, SOURCE, sources))
            assert execute_claim(db, claim(db, fresh, "test-llm", sources))
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
    assert len(calls) == 2 and llm.calls == 1
    with Session(db) as s:
        final = s.scalar(select(FinalScoreResult))
        assert final is not None
        new_raw = s.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == jobs[SOURCE], RawEvaluation.status == "READY"
            )
        )
        assert str(new_raw.submission_id) == sid
        assert new_raw.payload["audio_evidence"]["recording_id"] == rid
        assert len(list(s.scalars(select(FinalScoreResult)))) == 1


def test_no_text_respects_downstream_rejection(db, flow):
    f, _, llm, _, sources, calls, response = flow()
    response["value"] = body("")
    assert execute_claim(db, claim(db, f, SOURCE, sources))
    assert claim(db, f, "test-llm", sources) is None
    assert len(calls) == 1 and llm.calls == 0
    with Session(db) as s:
        assert s.get(EvaluationJob, f["jobs"]["test-llm"]).status == "WAITING"


def test_provider_unknown_is_not_retried_by_scans(db, flow):
    f, _, llm, _, sources, calls, response = flow()
    response.update(status=503, value={"code": "InternalError"})
    for _ in range(3):
        run_once(
            db, registry=f["registry"], adapters=sources, lease=timedelta(minutes=1), batch_size=10
        )
    with Session(db) as s:
        asr = s.get(EvaluationJob, f["jobs"][SOURCE])
        assert (asr.status, asr.error_code) == ("UNKNOWN", "EXTERNAL_OUTCOME_UNKNOWN")
        assert s.get(EvaluationJob, f["jobs"]["test-llm"]).status == "WAITING"
    assert len(calls) == 1 and llm.calls == 0
