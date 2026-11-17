"""The paid chain's wiring, exercised with a transport that cannot access the network."""

from types import SimpleNamespace

import dashscope
import httpx
import pytest
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
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(make_wav(rate=8000))
    monkeypatch.setenv("EVAL02E_AUDIO_FILE", str(audio))
    monkeypatch.setenv("EVAL02E_PRIVATE_REVIEW_FILE", str(tmp_path / "private-review.json"))
    monkeypatch.setattr(
        real_qa_chain,
        "make_url",
        lambda _: SimpleNamespace(host="127.0.0.1", database="vnext_eval02e_test"),
    )
    sdk_client = dashscope.Client
    response = {"asr": asr_body("虚构的回答"), "status": 200}

    def handler(request):
        if "multimodal-generation" in request.url.path:
            return httpx.Response(response["status"], json=response["asr"])
        return httpx.Response(200, json=llm_body())

    def sdk_without_network(**kwargs):
        original_http = kwargs.pop("http_client")
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


def test_asr_failure_never_calls_llm(guarded_flow, offline_configuration, capsys):
    from manual import real_qa_chain

    offline_configuration.update(status=503, asr={"code": "InternalError"})
    with pytest.raises(pytest.fail.Exception):
        real_qa_chain.test_authorized_provider_chain(guarded_flow)
    output = capsys.readouterr().out
    assert '"asr_calls": 1' in output and '"llm_calls": 0' in output
    assert '"asr_job_status": "UNKNOWN"' in output
