"""The paid chain's wiring, exercised with a transport that cannot access the network."""

from datetime import timedelta

import dashscope
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_audio_preparation import make_wav
from test_dashscope_asr import body as asr_body
from test_qwen_qa_text import response as llm_body

pytest_plugins = ("test_asr_dependency", "manual.real_qa_chain")


@pytest.fixture(autouse=True)
def offline_configuration(monkeypatch, tmp_path):
    from manual import real_qa_chain

    monkeypatch.setenv("EVAL02E_PAID_CALLS", "ASR1_LLM1")
    monkeypatch.setenv("EVAL02E_NON_CHILD_AUDIO", "confirmed")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EVAL02E_REGION", "cn-beijing")
    monkeypatch.setenv("EVAL02E_WORKSPACE", "test-workspace")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(make_wav(rate=8000))
    monkeypatch.setenv("EVAL02E_AUDIO_FILE", str(audio))
    monkeypatch.setenv("EVAL02E_PRIVATE_REVIEW_FILE", str(tmp_path / "private-review.json"))
    validator = real_qa_chain._validate_local_database
    monkeypatch.setattr(real_qa_chain, "_validate_local_database", lambda _: None)
    sdk_client = dashscope.Client
    response = {
        "asr": asr_body("虚构的回答"),
        "status": 200,
        "llm_status": 200,
        "llm": llm_body(),
        "validator": validator,
    }

    def handler(request):
        if "multimodal-generation" in request.url.path:
            if response.get("asr_hook"):
                response["asr_hook"]()
            return httpx.Response(response["status"], json=response["asr"])
        return httpx.Response(response["llm_status"], json=response["llm"])

    def sdk_without_network(**kwargs):
        original_http = kwargs.pop("http_client")
        assert original_http.trust_env is False
        hooks = original_http.event_hooks
        original_http.close()
        return sdk_client(
            **kwargs,
            http_client=httpx.Client(transport=httpx.MockTransport(handler), event_hooks=hooks),
        )

    monkeypatch.setattr(real_qa_chain.dashscope, "Client", sdk_without_network)
    return response


def test_real_chain_entry_uses_only_mock_transport(guarded_flow, capsys, tmp_path):
    import json

    from manual import real_qa_chain

    real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert '"asr_calls": 1' in output and '"llm_calls": 1' in output
    assert '"linked": true' in output and '"formal_result": false' in output
    assert "虚构的回答" not in output and "test-key-not-real" not in output
    report = json.loads((tmp_path / "private-review.json").read_text(encoding="utf-8"))
    assert report["asr_text"] == "虚构的回答"
    assert report["evaluation"]["dimension_reasons"]


def test_no_text_never_calls_llm(guarded_flow, offline_configuration, capsys, tmp_path):
    from manual import real_qa_chain

    offline_configuration["asr"] = asr_body("")
    real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert '"asr_calls": 1' in output and '"llm_calls": 0' in output
    assert '"asr_status": "NO_TEXT"' in output and '"formal_result": false' in output
    assert not (tmp_path / "private-review.json").exists()


@pytest.mark.parametrize(
    "status,body,expected",
    [(503, {"code": "InternalError"}, "UNKNOWN"), (401, {"code": "InvalidApiKey"}, "FAILED")],
)
def test_asr_failure_never_calls_llm(
    guarded_flow, offline_configuration, capsys, status, body, expected
):
    from manual import real_qa_chain

    offline_configuration.update(status=status, asr=body)
    with pytest.raises(pytest.fail.Exception):
        real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert '"asr_calls": 1' in output and '"llm_calls": 0' in output
    assert f'"asr_job_status": "{expected}"' in output
    from app.scoring.models import FinalScoreResult

    with Session(guarded_flow[0]) as session:
        assert session.scalar(select(FinalScoreResult)) is None


def test_llm_failure_never_creates_formal_result(guarded_flow, offline_configuration, capsys):
    from manual import real_qa_chain

    from app.scoring.models import FinalScoreResult

    offline_configuration.update(llm_status=503, llm={"code": "InternalError"})
    with pytest.raises(pytest.fail.Exception):
        real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert '"asr_calls": 1' in output and '"llm_calls": 1' in output
    assert '"llm_status": "UNKNOWN"' in output
    with Session(guarded_flow[0]) as session:
        assert session.scalar(select(FinalScoreResult)) is None


def test_elapsed_preparation_and_provider_time_fits_lease(
    guarded_flow, offline_configuration, monkeypatch, capsys
):
    from manual import real_qa_chain

    import app.evaluation.dashscope_asr as asr_module

    db, flow, _, _ = guarded_flow
    job_id = flow["jobs"][real_qa_chain.ASR_SOURCE]
    original = asr_module.prepare_audio
    elapsed = []

    def advance(label):
        with db.begin() as connection:
            connection.execute(
                text(
                    "UPDATE evaluation_jobs SET lease_until=lease_until-interval '40 seconds' WHERE job_id=:id"
                ),
                {"id": job_id},
            )
        elapsed.append(label)

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        advance("audio")
        return result

    monkeypatch.setattr(asr_module, "prepare_audio", prepare)
    offline_configuration["asr_hook"] = lambda: advance("provider")
    assert real_qa_chain.MANUAL_LEASE > timedelta(seconds=80)
    real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert elapsed == ["audio", "provider"]
    assert '"asr_job_status": "READY"' in output and '"llm_status": "READY"' in output
    assert '"asr_calls": 1' in output and '"llm_calls": 1' in output


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://x:y@127.0.0.1:5432/vnext_eval02e_test?host=192.0.2.1",
        "postgresql+psycopg://x:y@127.0.0.1:5432/vnext_eval02e_test?hostaddr=192.0.2.1",
        "postgresql+psycopg://x:y@127.0.0.1:5432/vnext_eval02e_test?options=-csearch_path%3Dpublic",
        "postgresql+psycopg://x:y@127.0.0.1:5432/development",
    ],
)
def test_database_override_rejected_before_any_connection(url, offline_configuration, monkeypatch):
    from manual import real_qa_chain

    attempts = []
    monkeypatch.setattr(real_qa_chain, "create_engine", lambda *args, **kwargs: attempts.append(1))
    with pytest.raises(ValueError):
        offline_configuration["validator"](url)
    assert attempts == []


def test_database_override_rejected_before_schema_fixture(
    offline_configuration, monkeypatch, tmp_path
):
    from manual import real_qa_chain

    monkeypatch.setattr(
        real_qa_chain, "_validate_local_database", offline_configuration["validator"]
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://x:y@127.0.0.1:5432/vnext_eval02e_test?host=192.0.2.1",
    )

    class Request:
        def getfixturevalue(self, name):
            pytest.fail("Database fixture must not be reached")

    with pytest.raises(pytest.fail.Exception, match="disposable local"):
        real_qa_chain.guarded_flow.__wrapped__(Request(), monkeypatch, tmp_path)


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS"])
def test_pg_environment_cannot_override_connection(offline_configuration, monkeypatch, name):
    monkeypatch.setenv(name, "unexpected-value")
    with pytest.raises(ValueError):
        offline_configuration["validator"](
            "postgresql+psycopg://x:y@127.0.0.1:5432/vnext_eval02e_test"
        )


def test_actual_disposable_database_identity(offline_configuration):
    import os

    from sqlalchemy.engine import make_url

    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or make_url(url).database != "vnext_eval02e_test":
        pytest.skip("Dedicated local evaluation database is not configured")
    offline_configuration["validator"](url)


def test_private_review_path_inside_repository_rejected(
    offline_configuration, monkeypatch, tmp_path
):
    from manual import real_qa_chain

    monkeypatch.setenv(
        "EVAL02E_PRIVATE_REVIEW_FILE",
        str(real_qa_chain.Path(__file__).resolve().parents[2] / "private-review.json"),
    )

    class Request:
        def getfixturevalue(self, name):
            pytest.fail("Database fixture must not be reached")

    with pytest.raises(pytest.fail.Exception, match="outside the repository"):
        real_qa_chain.guarded_flow.__wrapped__(Request(), monkeypatch, tmp_path)


def test_missing_authorization_exits_before_fixture_or_network(monkeypatch, tmp_path):
    from manual import real_qa_chain

    monkeypatch.delenv("EVAL02E_PAID_CALLS")

    class Request:
        def getfixturevalue(self, name):
            pytest.fail("Database fixture must not be reached")

    with pytest.raises(pytest.skip.Exception):
        real_qa_chain.guarded_flow.__wrapped__(Request(), monkeypatch, tmp_path)
