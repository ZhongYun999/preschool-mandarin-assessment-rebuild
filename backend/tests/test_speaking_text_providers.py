"""Offline supplier transport and frozen Speaking evaluation integration."""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import dashscope
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_speaking_evaluation_input import claim, make_case
from test_speaking_formal_asr import ASR, CONTRACTS, MultiASR
from test_speaking_process_asr import REQUIREMENT, FakeASR, segment
from test_speaking_text_evaluation import RESULT

from app.evaluation.models import EvaluationJob, RawEvaluation
from app.evaluation.speaking_text_contract import PROMPT, PROMPT_SHA256, PROMPT_VERSION
from app.evaluation.speaking_text_execution_contract import (
    DEEPSEEK_ENDPOINT,
    DEEPSEEK_SOURCE,
    QWEN_SOURCE,
    VERSION,
)
from app.evaluation.speaking_text_providers import DeepSeekSpeakingText, QwenSpeakingText
from app.evaluation.worker import authorize_retry, execute_claim, recover_expired, schedule
from app.scoring.registry import PRODUCTION_REGISTRY, SPEAKING_TEXT_CONTRACTS
from app.task_types.speaking_asr_worker import claim as claim_process_asr
from app.task_types.speaking_asr_worker import execute as execute_process_asr

pytest_plugins = ("test_speaking_process_asr",)


def parameters(provider):
    common = {
        "provider": provider,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "response_format": "json_object",
        "max_tokens": 1024,
    }
    if provider == "dashscope":
        return {
            **common,
            "model": "qwen-plus",
            "api_version": "dashscope-chat-json-v1",
            "endpoint": "https://test-workspace.cn-beijing.maas.aliyuncs.com/api/v1",
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "enable_thinking": False,
            "temperature": 0.2,
        }
    return {
        **common,
        "model": "deepseek-v4-pro",
        "api_version": "deepseek-chat-json-v1",
        "endpoint": DEEPSEEK_ENDPOINT,
        "thinking": "disabled",
        "temperature": 0.3,
    }


def requirement(provider):
    return {
        "source": QWEN_SOURCE if provider == "dashscope" else DEEPSEEK_SOURCE,
        "version": VERSION,
        "parameters": parameters(provider),
        "depends_on": {"requirement_index": 0, "source": ASR["source"], "version": "1"},
    }


def response(provider, content=None, finish="stop"):
    if content is None:
        content = json.dumps(RESULT, ensure_ascii=False)
    choice = {"finish_reason": finish, "message": {"role": "assistant", "content": content}}
    if provider == "dashscope":
        return {
            "request_id": "qwen-request",
            "output": {"choices": [choice]},
            "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
        }
    return {
        "id": "deepseek-request",
        "model": "deepseek-v4-pro",
        "choices": [choice],
        "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
    }


def adapter(provider, handler):
    transport = httpx.MockTransport(handler)
    if provider == "dashscope":
        return QwenSpeakingText(
            key_provider=lambda: "qwen-test-secret",
            client_factory=lambda **kw: dashscope.Client(
                **kw, http_client=httpx.Client(transport=transport)
            ),
        )
    return DeepSeekSpeakingText(
        key_provider=lambda: "deepseek-test-secret",
        client_factory=lambda **kw: httpx.Client(transport=transport, **kw),
    )


def setup_case(db, client, flow, monkeypatch, provider, count=2):
    req = requirement(provider)
    selected = next(
        contract for contract in SPEAKING_TEXT_CONTRACTS if contract.source == req["source"]
    )
    assert PRODUCTION_REGISTRY.supports("speaking", req)
    return make_case(db, client, flow, monkeypatch, count, [ASR, req], [CONTRACTS[0], selected])


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize("count", [2, 3])
def test_actual_transport_and_frozen_full_chain(
    db, client, flow, monkeypatch, caplog, provider, count
):
    _, _, _, _, sid, registry, store, ids = setup_case(
        db, client, flow, monkeypatch, provider, count
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response(provider))

    client_adapter = adapter(provider, handle)
    source = requirement(provider)["source"]
    assert claim(db, ids[1], registry, store, client_adapter, source) is None
    assert calls == []
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    messages = sent["input"]["messages"] if provider == "dashscope" else sent["messages"]
    assert messages[0]["content"] == PROMPT
    contents = json.loads(messages[1]["content"])
    assert [s["text"] for s in contents["asr"]["segments"]] == [
        f"fixture-{i}" for i in range(count)
    ]
    assert "effective_help_count" not in json.dumps(contents)
    assert "replay_count" not in json.dumps(contents)
    assert sent["model"] == parameters(provider)["model"]
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
            )
        )
        assert (
            raw.submission_id == sid
            and raw.provider == provider
            and raw.model == parameters(provider)["model"]
        )
        assert (
            raw.payload["evaluation"] == RESULT and raw.input_fingerprint == job.input_fingerprint
        )
        assert raw.payload["prompt_sha256"] == PROMPT_SHA256
        assert job.requirement["parameters"] == parameters(provider)
        serialized = json.dumps(
            {"refs": raw.input_refs, "payload": raw.payload, "requirement": job.requirement}
        )
        assert "qwen-test-secret" not in serialized and "deepseek-test-secret" not in serialized
    assert "qwen-test-secret" not in caplog.text and "deepseek-test-secret" not in caplog.text
    assert claim(db, ids[1], registry, store, client_adapter, source) is None
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize(
    "content,finish",
    [
        ("not json", "stop"),
        ("  ", "stop"),
        ('{"x":1,"x":2}', "stop"),
        ('{"content_coherence_score":NaN}', "stop"),
        ('{"content_coherence_score":Infinity}', "stop"),
        ("```json\n{}\n```", "stop"),
        (json.dumps({**RESULT, "content_coherence_score": 0.6}), "stop"),
        (json.dumps({**RESULT, "review_recommended": "true"}), "stop"),
        (json.dumps({**RESULT, "dimension_reasons": "not an object"}), "stop"),
        (json.dumps({k: v for k, v in RESULT.items() if k != "content_coherence_score"}), "stop"),
        (json.dumps({**RESULT, "final_score": 5}), "stop"),
        (json.dumps(RESULT), "length"),
    ],
)
def test_invalid_model_response_is_never_ready(
    db, client, flow, monkeypatch, provider, content, finish
):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response(provider, content, finish))

    source = requirement(provider)["source"]
    assert execute_claim(db, claim(db, ids[1], registry, store, adapter(provider, handle), source))
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "INVALID"
        assert (
            session.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
                )
            )
            is None
        )
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_insufficient_evidence_is_not_ready(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    payload = '{"status":"INSUFFICIENT_EVIDENCE","reasons":["无法判断图片内容"]}'
    provider_adapter = adapter(
        provider, lambda request: httpx.Response(200, json=response(provider, payload))
    )
    assert execute_claim(
        db, claim(db, ids[1], registry, store, provider_adapter, requirement(provider)["source"])
    )
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.status == "FAILED" and job.error_code == "LLM_INSUFFICIENT_EVIDENCE"
        raw = session.scalar(select(RawEvaluation).where(RawEvaluation.job_id == ids[1]))
        assert raw.status == "FAILED" and raw.payload is None


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_model_refusal_is_invalid(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    body = response(provider)
    choices = body["output"]["choices"] if provider == "dashscope" else body["choices"]
    choices[0]["message"]["refusal"] = "declined"
    client_adapter = adapter(provider, lambda request: httpx.Response(200, json=body))
    assert execute_claim(
        db, claim(db, ids[1], registry, store, client_adapter, requirement(provider)["source"])
    )
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "INVALID"


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_server_failure_is_unknown_and_not_retried(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "unavailable"})

    client_adapter = adapter(provider, handle)
    source = requirement(provider)["source"]
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    assert claim(db, ids[1], registry, store, client_adapter, source) is None
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "UNKNOWN"
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_timeout_requires_explicit_unknown_authorization(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("offline")
        return httpx.Response(200, json=response(provider))

    source = requirement(provider)["source"]
    client_adapter = adapter(provider, handle)
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    assert claim(db, ids[1], registry, store, client_adapter, source) is None
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[1])
        assert job.status == "UNKNOWN"
        frozen = deepcopy(job.requirement)
        with pytest.raises(ValueError, match="explicit"):
            authorize_retry(session, job.job_id)
        authorize_retry(session, job.job_id, accept_unknown=True)
        assert job.requirement == frozen
    assert len(calls) == 1
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    assert len(calls) == 2
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.status == "READY" and job.requirement == frozen


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "LLM_REQUEST_REJECTED"),
        (401, "LLM_AUTHENTICATION"),
        (403, "LLM_PERMISSION"),
        (404, "LLM_MODEL_UNAVAILABLE"),
        (429, "LLM_RATE_LIMIT"),
    ],
)
def test_known_http_failures(db, client, flow, monkeypatch, provider, status, expected):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, json={"code": "InvalidParameter" if status == 400 else "other"}
        )

    assert execute_claim(
        db,
        claim(
            db, ids[1], registry, store, adapter(provider, handle), requirement(provider)["source"]
        ),
    )
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.status == "FAILED" and job.error_code == expected
    assert len(calls) == 1


def test_execution_capability_and_prompt_parameters_are_frozen():
    qwen, deepseek = requirement("dashscope"), requirement("deepseek")
    assert PRODUCTION_REGISTRY.supports("speaking", qwen)
    assert PRODUCTION_REGISTRY.supports("speaking", deepseek)
    assert PRODUCTION_REGISTRY.resolve("test-only", "1") is None
    for key, bad in (
        ("prompt_sha256", "0" * 64),
        ("model", "unknown-model"),
        ("endpoint", "https://example.com"),
        ("enable_thinking", True),
    ):
        changed = deepcopy(qwen)
        changed["parameters"][key] = bad
        assert not PRODUCTION_REGISTRY.supports("speaking", changed)
    changed = deepcopy(deepseek)
    changed["parameters"]["model"] = "deepseek-flash"
    assert not PRODUCTION_REGISTRY.supports(
        "speaking", changed
    )  # its effective temperature is fixed at 1
    changed["parameters"]["temperature"] = 1
    assert PRODUCTION_REGISTRY.supports("speaking", changed)
    formal_asr = {
        "source": "dashscope-speaking-qwen3-asr",
        "version": "1",
        "parameters": {
            "provider": "dashscope",
            "model": "qwen3-asr-flash",
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "enable_itn": False,
            "language": "zh",
            "processing_version": "1",
            "confirmed_no_text": False,
            "audio_spec": {
                "container": "wav",
                "codec": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "processing_version": "1",
                "ffmpeg_version": "fixture",
                "ffprobe_version": "fixture",
            },
        },
    }
    for downstream in (qwen, deepseek):
        downstream = deepcopy(downstream)
        downstream["depends_on"]["source"] = formal_asr["source"]
        assert PRODUCTION_REGISTRY.valid_plan("speaking", [formal_asr, downstream])


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_all_no_text_never_sends_provider_request(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(no_text=True, store=store), ASR["source"])
    )
    calls = []
    source = requirement(provider)["source"]
    client_adapter = adapter(provider, lambda request: calls.append(request))
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    assert calls == []
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).error_code == "LLM_INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_undispatched_crash_can_recover_same_snapshot(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response(provider))

    source = requirement(provider)["source"]
    client_adapter = adapter(provider, handle)
    old = claim(db, ids[1], registry, store, client_adapter, source)
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[1])
        fingerprint, refs = job.input_fingerprint, deepcopy(job.input_refs)
        job.lease_until = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        assert recover_expired(session) >= 1
        assert session.get(EvaluationJob, ids[1]).status == "WAITING"
    assert not execute_claim(db, old) and calls == []
    assert execute_claim(db, claim(db, ids[1], registry, store, client_adapter, source))
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.input_fingerprint == fingerprint and job.input_refs == refs
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize("damage", ["order", "missing", "foreign", "fingerprint"])
def test_altered_asr_snapshot_never_dispatches(db, client, flow, monkeypatch, provider, damage):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 2)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []
    work = claim(
        db,
        ids[1],
        registry,
        store,
        adapter(provider, lambda request: calls.append(request)),
        requirement(provider)["source"],
    )
    segments = work.inputs.payload["asr"]["segments"]
    if damage == "order":
        segments.reverse()
    elif damage == "missing":
        segments.pop()
    elif damage == "foreign":
        segments[0]["recording_id"] = "not-this-submission"
    else:
        work.inputs.refs["asr"]["input_fingerprint"] = "bad-fingerprint"
    try:
        assert not execute_claim(db, work)
    except ValueError as exc:
        assert "frozen input identity" in str(exc)
    assert calls == []


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_dispatched_crash_becomes_unknown_without_repeat(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []
    client_adapter = adapter(provider, lambda request: calls.append(request))
    source = requirement(provider)["source"]
    old = claim(db, ids[1], registry, store, client_adapter, source)
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[1])
        now = session.scalar(select(func.clock_timestamp()))
        job.dispatch_started_at = now
        job.lease_until = now - timedelta(seconds=1)
    with Session(db) as session, session.begin():
        assert recover_expired(session) >= 1
        assert session.get(EvaluationJob, ids[1]).status == "UNKNOWN"
    assert not execute_claim(db, old)
    assert claim(db, ids[1], registry, store, client_adapter, source) is None
    assert calls == []


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_closed_session_uses_frozen_submission(db, client, flow, monkeypatch, provider):
    root, _, headers, _, _, registry, store, ids = setup_case(
        db, client, flow, monkeypatch, provider, 1
    )
    assert (
        client.post(
            root + "/close", headers=headers, json={"close_request_id": "close"}
        ).status_code
        == 200
    )
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response(provider))

    source = requirement(provider)["source"]
    assert execute_claim(db, claim(db, ids[1], registry, store, adapter(provider, handle), source))
    assert len(calls) == 1
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "READY"


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_reported_model_mismatch_is_not_accepted(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    body = response(provider)
    body["model"] = "unexpected-model"
    client_adapter = adapter(provider, lambda request: httpx.Response(200, json=body))
    assert execute_claim(
        db, claim(db, ids[1], registry, store, client_adapter, requirement(provider)["source"])
    )
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).status == "INVALID"
        assert (
            session.scalar(
                select(RawEvaluation).where(
                    RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
                )
            )
            is None
        )


def test_retry_cannot_switch_supplier(db, client, flow, monkeypatch):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, "dashscope", 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(400, json={"code": "InvalidParameter"})

    assert execute_claim(
        db, claim(db, ids[1], registry, store, adapter("dashscope", reject), QWEN_SOURCE)
    )
    with Session(db) as session, session.begin():
        job = session.get(EvaluationJob, ids[1])
        original = deepcopy(job.requirement)
        assert job.status == "FAILED"
        authorize_retry(session, job.job_id)
    wrong = adapter("deepseek", lambda request: calls.append(request))
    with pytest.raises(ValueError, match="execution contract"):
        claim(db, ids[1], registry, store, wrong, QWEN_SOURCE)
    with Session(db) as session:
        assert session.get(EvaluationJob, ids[1]).requirement == original
    assert len(calls) == 1


def test_redo_cannot_reuse_old_provider_evidence(db, client, flow, monkeypatch):
    root, base, headers, old_attempt, old_submission, registry, store, ids = setup_case(
        db, client, flow, monkeypatch, "dashscope", 1
    )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response("dashscope"))

    provider_adapter = adapter("dashscope", handle)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, ids[1], registry, store, provider_adapter, QWEN_SOURCE))
    redo = client.post(
        base + "/redo",
        headers=headers,
        json={"redo_request_id": "redo", "expected_attempt_id": old_attempt},
    )
    assert redo.status_code == 200
    attempt = redo.json()["attempt_id"]
    playback = client.post(
        base + "/playbacks",
        headers=headers,
        json={"attempt_id": attempt, "request_id": "prompt", "kind": "INITIAL_PROMPT"},
    )
    assert playback.status_code == 200
    assert (
        client.post(
            base + "/playbacks/" + playback.json()["playback_id"] + "/finish",
            headers=headers,
            json={"attempt_id": attempt, "completed": True},
        ).status_code
        == 200
    )
    operation, _ = segment(client, root, base, headers, attempt, "new-record")
    assert execute_process_asr(db, claim_process_asr(db, operation, REQUIREMENT), FakeASR())
    submitted = client.post(
        base + "/submissions/speaking",
        headers=headers,
        json={"attempt_id": attempt, "finish_request_id": "new-finish"},
    )
    assert submitted.status_code == 200
    new_submission = UUID(submitted.json()["submission_id"])
    with Session(db) as session, session.begin():
        assert schedule(session) == 2
        jobs = list(
            session.scalars(
                select(EvaluationJob)
                .where(EvaluationJob.submission_id == new_submission)
                .order_by(EvaluationJob.requirement_index)
            )
        )
        new_asr, new_text = [job.job_id for job in jobs]
        old = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == ids[1], RawEvaluation.status == "READY"
            )
        )
        assert old.submission_id == old_submission
        old_id = old.evaluation_id
    assert claim(db, new_text, registry, store, provider_adapter, QWEN_SOURCE) is None
    assert execute_claim(
        db, claim(db, new_asr, registry, store, MultiASR(store=store), ASR["source"])
    )
    assert execute_claim(db, claim(db, new_text, registry, store, provider_adapter, QWEN_SOURCE))
    assert len(calls) == 2
    with Session(db) as session:
        raw = session.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == new_text, RawEvaluation.status == "READY"
            )
        )
        assert raw.submission_id == new_submission and raw.evaluation_id != old_id


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_explicit_quota_rejection_is_known_failure(db, client, flow, monkeypatch, provider):
    _, _, _, _, _, registry, store, ids = setup_case(db, client, flow, monkeypatch, provider, 1)
    assert execute_claim(
        db, claim(db, ids[0], registry, store, MultiASR(store=store), ASR["source"])
    )
    handler = lambda request: httpx.Response(402, json={"code": "Arrearage"})
    assert execute_claim(
        db,
        claim(
            db, ids[1], registry, store, adapter(provider, handler), requirement(provider)["source"]
        ),
    )
    with Session(db) as session:
        job = session.get(EvaluationJob, ids[1])
        assert job.status == "FAILED" and job.error_code == "LLM_QUOTA"
