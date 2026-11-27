"""Offline provider credential lifecycle against isolated PostgreSQL schemas."""

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation import provider_config as service
from app.evaluation.provider_models import ProviderAudit, ProviderCredential

pytest_plugins = ("test_qa_formal_text_contract",)


@pytest.fixture
def master(monkeypatch):
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_config_rotation_switch_disable_and_no_fallback(db, master, monkeypatch):
    with Session(db) as s, s.begin():
        first = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            api_key="private-qwen-key",
        )
        first_id = first.credential_id
        assert first.revision == 1
    with Session(db) as s, s.begin():
        second = service.save(
            s, provider="deepseek", model="deepseek-flash", api_key="private-deepseek-key"
        )
        second_id = second.credential_id
        assert second.revision == 2
    with Session(db) as s, s.begin():
        third = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            credential_id=first_id,
        )
        assert third.revision == 3
        config, credential, value = service.resolve(s)
        assert (
            config.credential_id == first_id
            and credential.version == 1
            and value == "private-qwen-key"
        )
    with Session(db) as s, s.begin():
        fourth = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            api_key="rotated-key",
        )
        new_id = fourth.credential_id
        assert s.get(ProviderCredential, new_id).version == 2
        assert b"rotated-key" not in s.get(ProviderCredential, new_id).ciphertext
    with Session(db) as s, s.begin():
        service.disable(s, new_id)
    with Session(db) as s, s.begin():
        with pytest.raises(service.CredentialUnavailable):
            service.resolve(s)
        with pytest.raises(service.CredentialUnavailable):
            service.save(
                s,
                provider="dashscope",
                model="qwen-plus",
                region="cn-beijing",
                workspace="test",
                credential_id=second_id,
            )
    with Session(db) as s:
        rows = list(s.scalars(select(ProviderAudit)))
        assert "private-qwen-key" not in json.dumps([r.action for r in rows])
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with Session(db) as s, s.begin(), pytest.raises(service.CredentialUnavailable):
        service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            credential_id=first_id,
        )


def test_dynamic_claim_snapshot_survives_switch_and_unknown_retry(db, qa_formal, monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from test_asr_dependency import claim

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import authorize_retry, execute_claim, recover_expired

    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    f, _, _, sources = qa_formal()
    with Session(db) as s, s.begin():
        service.save(s, provider="deepseek", model="deepseek-flash", api_key="first-key")
    dynamic = DynamicQAText()
    sources[(business.SOURCE, business.VERSION)] = dynamic
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))
    old = claim(db, f, business.SOURCE, sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, old.job_id)
        old_fingerprint = job.input_fingerprint
        assert job.execution_snapshot["provider"] == "deepseek"
        assert "first-key" not in json.dumps(job.execution_snapshot)
    with Session(db) as s, s.begin():
        service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            api_key="second-key",
        )
    assert old.adapter.snapshot["provider"] == "deepseek"
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE evaluation_jobs SET dispatch_started_at=clock_timestamp(), lease_until=clock_timestamp()-interval '1 second' WHERE job_id=:id"
            ),
            {"id": old.job_id},
        )
    with Session(db) as s, s.begin():
        assert recover_expired(s) == 1
    with Session(db) as s, s.begin():
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == old.job_id))
        assert raw.status == "UNKNOWN" and raw.execution_snapshot["provider"] == "deepseek"
        with pytest.raises(ValueError):
            authorize_retry(s, old.job_id)
        authorize_retry(s, old.job_id, accept_unknown=True)
    new = claim(db, f, business.SOURCE, sources)
    assert new.adapter.snapshot["provider"] == "dashscope"
    with Session(db) as s:
        assert s.get(EvaluationJob, new.job_id).input_fingerprint == old_fingerprint
        assert (
            s.scalar(
                select(RawEvaluation).where(RawEvaluation.job_id == old.job_id)
            ).execution_snapshot["provider"]
            == "deepseek"
        )


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
def test_provider_mock_persists_identical_business_result(db, qa_formal, monkeypatch, provider):
    import socket

    import dashscope
    import httpx
    from cryptography.fernet import Fernet
    from test_asr_dependency import claim
    from test_qa_formal_text_contract import RESULT

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import execute_claim

    def block_network(*args, **kwargs):
        raise AssertionError("Unexpected network access in offline provider test")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    f, _, _, sources = qa_formal()
    options = {"region": "cn-beijing", "workspace": "test"} if provider == "dashscope" else {}
    model = "qwen-plus" if provider == "dashscope" else "deepseek-flash"
    with Session(db) as s, s.begin():
        service.save(s, provider=provider, model=model, api_key="fake-secret", **options)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        choice = {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(RESULT)},
        }
        if provider == "deepseek":
            return httpx.Response(
                200, json={"id": "test-id", "model": model, "choices": [choice], "usage": {}}
            )
        return httpx.Response(
            200,
            json={
                "request_id": "test-id",
                "model": model,
                "output": {"choices": [choice]},
                "usage": {},
            },
        )

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        if provider == "dashscope":
            return dashscope.Client(**kwargs, http_client=httpx.Client(transport=transport))
        return httpx.Client(transport=transport, **kwargs)

    sources[(business.SOURCE, business.VERSION)] = DynamicQAText(
        client_factories={provider: factory}
    )
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))
    assert execute_claim(db, claim(db, f, business.SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        assert raw.status == "READY" and raw.payload["evaluation"] == RESULT
        assert raw.execution_snapshot == job.execution_snapshot
        assert raw.execution_snapshot["provider"] == provider
        assert "fake-secret" not in json.dumps(raw.execution_snapshot)
    assert len(seen) == 1
    messages = seen[0]["input"]["messages"] if provider == "dashscope" else seen[0]["messages"]
    assert messages[0]["content"] == business.PROMPT
    assert json.loads(messages[1]["content"])["asr_text"] == "transcribed words"


def test_management_api_auth_and_secret_redaction(db, client, master, caplog):
    from test_teacher_api import AUTH

    path = "/teacher/llm-provider"
    assert client.get(path).status_code == 401
    payload = {
        "provider": "dashscope",
        "model": "qwen-plus",
        "region": "cn-beijing",
        "workspace": "test",
        "api_key": "sensitive-fixture-key",
    }
    response = client.put(path, headers=AUTH, json=payload)
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert "sensitive-fixture-key" not in response.text
    assert "sensitive-fixture-key" not in client.get(path, headers=AUTH).text
    assert "sensitive-fixture-key" not in client.get(path + "/credentials", headers=AUTH).text
    invalid = client.put(
        path, headers=AUTH, json={**payload, "provider": ["sensitive-fixture-key"]}
    )
    assert invalid.status_code == 422 and "sensitive-fixture-key" not in invalid.text
    assert "sensitive-fixture-key" not in caplog.text
    credential_id = response.json()["credential_id"]
    assert (
        client.post(path + f"/credentials/{credential_id}/disable", headers=AUTH).status_code == 200
    )
    assert client.get(path, headers=AUTH).json()["available"] is False


@pytest.mark.parametrize("provider", ["dashscope", "deepseek"])
@pytest.mark.parametrize(
    "case",
    [
        "invalid",
        "duplicate",
        "bad_band",
        "insufficient",
        "model_mismatch",
        "authentication",
        "quota",
        "rate_limit",
        "timeout",
    ],
)
def test_provider_failures_are_not_ready(provider, case, monkeypatch):
    import socket

    import dashscope
    import httpx
    from test_qa_formal_text_contract import RESULT, TEXT

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.qa_text_providers import BoundQAAdapter
    from app.evaluation.worker import InputSnapshot, KnownFailure

    def block_network(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    model = "qwen-plus" if provider == "dashscope" else "deepseek-flash"
    p = {
        "provider": provider,
        "model": model,
        "endpoint": "https://offline.invalid",
        "region": "cn-beijing",
        "workspace": "test",
        "api_version": "v1",
    }
    p["request_parameters"] = {
        "temperature": 1,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "stream": False,
        **(
            {"enable_thinking": False}
            if provider == "dashscope"
            else {"thinking": {"type": "disabled"}}
        ),
    }
    refs = {
        "asr": {
            "source": TEXT["depends_on"]["source"],
            "version": "1",
            **{
                k: "id"
                for k in (
                    "job_id",
                    "evaluation_id",
                    "input_fingerprint",
                    "submission_id",
                    "paper_entry_id",
                    "task_revision_id",
                    "recording_id",
                )
            },
        }
    }
    inputs = InputSnapshot(
        refs,
        {"asr": {"status": "TEXT", "text": "hi"}, "context": {}, "system_prompt": business.PROMPT},
    )
    content = json.dumps(RESULT)
    if case == "invalid":
        content = "not json"
    if case == "duplicate":
        content = '{"x":1,"x":2}'
    if case == "bad_band":
        content = json.dumps({**RESULT, "semantic_score": 1.7})
    if case == "insufficient":
        content = '{"status":"INSUFFICIENT_EVIDENCE","reasons":["no evidence"]}'
    status = {
        "authentication": 401,
        "quota": 402 if provider == "deepseek" else 403,
        "rate_limit": 429,
    }.get(case, 200)

    def handler(request):
        if case == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if status != 200:
            return httpx.Response(
                status, json={"code": "Arrearage" if case == "quota" else "error"}
            )
        choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        reported = "wrong-model" if case == "model_mismatch" else model
        if provider == "dashscope":
            return httpx.Response(
                200, json={"request_id": "id", "model": reported, "output": {"choices": [choice]}}
            )
        return httpx.Response(200, json={"id": "id", "model": reported, "choices": [choice]})

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        if provider == "dashscope":
            return dashscope.Client(**kwargs, http_client=httpx.Client(transport=transport))
        return httpx.Client(transport=transport, **kwargs)

    adapter = BoundQAAdapter(p, "fake-key", client_factory=factory)
    if case in {"authentication", "quota", "rate_limit", "insufficient"}:
        with pytest.raises(KnownFailure) as raised:
            adapter.evaluate(inputs, TEXT)
        assert (
            raised.value.code
            == {
                "authentication": "LLM_AUTHENTICATION",
                "quota": "LLM_QUOTA",
                "rate_limit": "LLM_RATE_LIMIT",
                "insufficient": "LLM_INSUFFICIENT_EVIDENCE",
            }[case]
        )
    elif case == "timeout":
        with pytest.raises(RuntimeError):
            adapter.evaluate(inputs, TEXT)
    else:
        assert not adapter.validate_result(adapter.evaluate(inputs, TEXT), TEXT)


def test_concurrent_credential_versions_are_serialized(db, master):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def create(key):
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            config = service.save(s, provider="deepseek", model="deepseek-flash", api_key=key)
            return config.credential_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, ["key-a", "key-b"]))
    with Session(db) as s:
        rows = list(s.scalars(select(ProviderCredential).order_by(ProviderCredential.version)))
        assert [row.version for row in rows] == [1, 2]
        assert {row.credential_id for row in rows} == set(ids)
        with s.begin_nested():
            config, credential, _ = service.resolve(s)
            assert config.credential_id == credential.credential_id


def test_concurrent_config_switch_and_claim_are_coherent(db, qa_formal, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from cryptography.fernet import Fernet
    from test_asr_dependency import claim

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import execute_claim

    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    f, _, _, sources = qa_formal()
    with Session(db) as s, s.begin():
        service.save(s, provider="deepseek", model="deepseek-flash", api_key="deepseek-key")
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))
    sources[(business.SOURCE, business.VERSION)] = DynamicQAText()
    barrier = Barrier(2)

    def switch():
        with Session(db) as s, s.begin():
            barrier.wait(timeout=10)
            config = service.save(
                s,
                provider="dashscope",
                model="qwen-plus",
                region="cn-beijing",
                workspace="test",
                api_key="qwen-key",
            )
            return config.revision

    def take():
        barrier.wait(timeout=10)
        return claim(db, f, business.SOURCE, sources)

    with ThreadPoolExecutor(max_workers=2) as pool:
        switch_future = pool.submit(switch)
        claim_future = pool.submit(take)
        assert switch_future.result(timeout=15) == 2
        work = claim_future.result(timeout=15)
    snap = work.adapter.snapshot
    assert (snap["provider"], snap["model"], snap["config_revision"], work.adapter.key) in {
        ("deepseek", "deepseek-flash", 1, "deepseek-key"),
        ("dashscope", "qwen-plus", 2, "qwen-key"),
    }
    with Session(db) as s:
        credential = s.get(ProviderCredential, snap["credential_id"])
        assert (
            credential.provider == snap["provider"]
            and credential.version == snap["credential_version"]
        )


def test_missing_config_blocks_dispatch(db, qa_formal):
    from test_asr_dependency import claim

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import execute_claim

    f, _, _, sources = qa_formal()
    sources[(business.SOURCE, business.VERSION)] = DynamicQAText()
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))
    assert claim(db, f, business.SOURCE, sources) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        assert (job.status, job.error_code, job.execution_snapshot) == (
            "WAITING",
            "LLM_PROVIDER_UNAVAILABLE",
            None,
        )
        assert s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id)) is None


def test_failed_retry_uses_new_provider_without_input_drift(db, qa_formal, monkeypatch):
    import socket

    import dashscope
    import httpx
    from cryptography.fernet import Fernet
    from test_asr_dependency import claim
    from test_qa_formal_text_contract import RESULT

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import authorize_retry, execute_claim

    def block_network(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    f, _, _, sources = qa_formal()
    with Session(db) as s, s.begin():
        service.save(s, provider="deepseek", model="deepseek-flash", api_key="key-a")
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))

    def deep_factory(**kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kwargs
        )

    sources[(business.SOURCE, business.VERSION)] = DynamicQAText(
        client_factories={"deepseek": deep_factory}
    )
    assert execute_claim(db, claim(db, f, business.SOURCE, sources))
    with Session(db) as s, s.begin():
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        fingerprint = job.input_fingerprint
        assert job.status == "FAILED"
        service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test",
            api_key="key-b",
        )
        authorize_retry(s, job.job_id)

    def handler(request):
        choice = {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(RESULT)},
        }
        return httpx.Response(200, json={"request_id": "id", "output": {"choices": [choice]}})

    def qwen_factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    sources[(business.SOURCE, business.VERSION)] = DynamicQAText(
        client_factories={"dashscope": qwen_factory}
    )
    assert execute_claim(db, claim(db, f, business.SOURCE, sources))
    assert claim(db, f, business.SOURCE, sources) is None
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        rows = list(s.scalars(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id)))
        assert job.input_fingerprint == fingerprint and job.status == "READY"
        assert {r.status: r.execution_snapshot["provider"] for r in rows} == {
            "FAILED": "deepseek",
            "READY": "dashscope",
        }


def test_database_preserves_credential_and_config_history(db, master):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with Session(db) as s, s.begin():
        config = service.save(s, provider="deepseek", model="deepseek-flash", api_key="private-key")
        credential_id, config_id = config.credential_id, config.config_id
    with db.connect() as c:
        with pytest.raises(DBAPIError):
            c.execute(
                text(
                    "UPDATE llm_provider_credentials SET ciphertext='bad' WHERE credential_id=:id"
                ),
                {"id": credential_id},
            )
        c.rollback()
        with pytest.raises(DBAPIError):
            c.execute(
                text("DELETE FROM llm_provider_configs WHERE config_id=:id"), {"id": config_id}
            )
        c.rollback()


def test_claim_executes_original_provider_after_switch_then_retry(db, qa_formal, monkeypatch):
    import socket

    import dashscope
    import httpx
    from cryptography.fernet import Fernet
    from test_asr_dependency import claim
    from test_qa_formal_text_contract import RESULT

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.qa_text_providers import DynamicQAText
    from app.evaluation.worker import authorize_retry, execute_claim

    def block_network(*args, **kwargs):
        raise AssertionError("Unexpected network access in offline provider test")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    f, _, _, sources = qa_formal()
    calls = []

    def deep_handler(request):
        calls.append(("deepseek", str(request.url), json.loads(request.content)))
        raise httpx.ReadTimeout("simulated timeout", request=request)

    def qwen_handler(request):
        calls.append(("qwen", str(request.url), json.loads(request.content)))
        choice = {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(RESULT)},
        }
        return httpx.Response(
            200,
            json={
                "request_id": "mock-request",
                "model": "qwen-plus",
                "output": {"choices": [choice]},
            },
        )

    def deep_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(deep_handler), **kwargs)

    def qwen_factory(**kwargs):
        return dashscope.Client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(qwen_handler))
        )

    sources[(business.SOURCE, business.VERSION)] = DynamicQAText(
        client_factories={"deepseek": deep_factory, "dashscope": qwen_factory}
    )
    with Session(db) as s, s.begin():
        old_config = service.save(
            s, provider="deepseek", model="deepseek-flash", api_key="synthetic-old-key"
        )
        old_revision = old_config.revision
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))
    old = claim(db, f, business.SOURCE, sources)
    with Session(db) as s:
        job = s.get(EvaluationJob, old.job_id)
        fingerprint, old_snapshot = job.input_fingerprint, dict(job.execution_snapshot)
        assert old_snapshot["config_revision"] == old_revision
    with Session(db) as s, s.begin():
        new_config = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="offline-test",
            api_key="synthetic-new-key",
        )
        new_revision = new_config.revision
    assert execute_claim(db, old)
    with Session(db) as s, s.begin():
        job = s.get(EvaluationJob, old.job_id)
        raw = s.scalar(
            select(RawEvaluation).where(
                RawEvaluation.job_id == old.job_id, RawEvaluation.status == "UNKNOWN"
            )
        )
        assert (
            job.status == "UNKNOWN"
            and raw.execution_snapshot == job.execution_snapshot == old_snapshot
        )
        assert raw.input_fingerprint == job.input_fingerprint == fingerprint
        assert "synthetic-old-key" not in json.dumps(raw.execution_snapshot)
        authorize_retry(s, old.job_id, accept_unknown=True)
    retry = claim(db, f, business.SOURCE, sources)
    assert retry.adapter.snapshot["provider"] == "dashscope"
    assert retry.adapter.snapshot["config_revision"] == new_revision
    assert execute_claim(db, retry)
    with Session(db) as s:
        job = s.get(EvaluationJob, old.job_id)
        rows = list(s.scalars(select(RawEvaluation).where(RawEvaluation.job_id == old.job_id)))
        by_status = {row.status: row for row in rows}
        assert set(by_status) == {"UNKNOWN", "READY"}
        assert by_status["UNKNOWN"].execution_snapshot == old_snapshot
        assert (
            by_status["READY"].execution_snapshot
            == job.execution_snapshot
            == retry.adapter.snapshot
        )
        assert by_status["READY"].payload["evaluation"] == RESULT
        assert all(row.input_fingerprint == fingerprint for row in rows)
        assert "synthetic-new-key" not in json.dumps(by_status["READY"].execution_snapshot)
    assert [item[0] for item in calls] == ["deepseek", "qwen"]
    assert calls[0][1] == "https://api.deepseek.com/chat/completions"
    assert calls[0][2]["model"] == "deepseek-flash"
    assert calls[0][2]["messages"][0]["content"] == business.PROMPT
    assert calls[1][1].startswith("https://offline-test.cn-beijing.maas.aliyuncs.com/")
    assert calls[1][2]["model"] == "qwen-plus"
    assert calls[1][2]["input"]["messages"][0]["content"] == business.PROMPT
    assert (
        json.loads(calls[0][2]["messages"][1]["content"])["asr_text"]
        == json.loads(calls[1][2]["input"]["messages"][1]["content"])["asr_text"]
    )
