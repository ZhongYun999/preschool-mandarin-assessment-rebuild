"""Offline request and response tests using the actual DashScope SDK transport."""

import base64
import hashlib
import json
from dataclasses import asdict

import dashscope
import httpx
import pytest
from test_audio_preparation import make_wav

from app.evaluation.asr_contract import MODEL, SOURCE, VERSION, valid_parameters
from app.evaluation.dashscope_asr import DashScopeASR
from app.evaluation.worker import InputSnapshot, KnownFailure
from app.media.audio_inspection import AudioTools
from app.media.audio_normalization import AudioSpec
from app.media.recording_store import RecordingStore
from app.scoring.registry import PRODUCTION_REGISTRY


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-real")
    tools = AudioTools()
    spec = AudioSpec(
        "wav", "pcm_s16le", 16000, 1, "asr-test-1", tools.version(), tools.version(probe=True)
    )
    p = {
        "model": MODEL,
        "region": "cn-beijing",
        "workspace": "test-workspace",
        "enable_itn": False,
        "language": None,
        "audio_spec": asdict(spec),
    }
    original, derived = RecordingStore(tmp_path / "original"), RecordingStore(tmp_path / "derived")
    data = make_wav(rate=8000)
    digest = hashlib.sha256(data).hexdigest()
    stream, temp = original.staging_file()
    with stream:
        stream.write(data)
    try:
        ref = original.commit(temp, digest)
    finally:
        temp.unlink()
    recording = {
        "recording_id": "recording-1",
        "sha256": digest,
        "media_type": "audio/mpeg",
        "storage_ref": ref,
    }
    inputs = InputSnapshot(
        refs={"endpoint": "https://test-workspace.cn-beijing.maas.aliyuncs.com/api/v1"},
        payload={"recording": recording},
    )
    requirement = {"source": SOURCE, "version": VERSION, "parameters": p}
    return tools, original, derived, inputs, requirement, data


def body(text="hello"):
    return {
        "request_id": "request-1",
        "output": {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": [{"text": text}]},
                }
            ]
        },
        "usage": {"input_tokens": 10, "seconds": 1},
    }


def make_adapter(setup, handler):
    tools, original, derived, _, _, _ = setup
    http = httpx.Client(transport=httpx.MockTransport(handler))

    def factory(**kwargs):
        return dashscope.Client(**kwargs, http_client=http)

    return DashScopeASR(original, derived, tools, client_factory=factory)


@pytest.mark.parametrize("text,status", [("你好，世界。", "TEXT"), ("", "NO_TEXT")])
def test_actual_sdk_request_and_response(setup, text, status, caplog):
    _, original, _, inputs, requirement, data = setup
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body(text))

    adapter = make_adapter(setup, handler)
    result = adapter.evaluate(inputs, requirement)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "test-workspace.cn-beijing.maas.aliyuncs.com"
    assert request.url.path == "/api/v1/services/aigc/multimodal-generation/generation"
    sent = json.loads(request.content)
    assert sent["model"] == MODEL
    assert sent["parameters"]["asr_options"] == {"enable_itn": False}
    uri = sent["input"]["messages"][0]["content"][0]["audio"]
    assert uri.startswith("data:audio/wav;base64,")
    audio = base64.b64decode(uri.split(",", 1)[1])
    assert (
        audio[:4] == b"RIFF"
        and hashlib.sha256(audio).hexdigest()
        == result.payload["audio_evidence"]["output"]["sha256"]
    )
    assert result.payload["status"] == status
    assert result.payload["text"] == (text if text else None)
    assert "confidence" not in result.payload
    assert result.payload["request_id"] == "request-1" and result.payload["usage"] == {
        "input_tokens": 10,
        "seconds": 1,
    }
    assert result.provider == "dashscope" and result.model == MODEL
    assert adapter.validate_result(result, requirement)
    assert (
        original.read(
            inputs.payload["recording"]["storage_ref"], inputs.payload["recording"]["sha256"]
        )
        == data
    )
    assert "test-key-not-real" not in caplog.text and uri not in caplog.text
    assert valid_parameters(requirement["parameters"])
    assert PRODUCTION_REGISTRY.supports("qa", requirement)


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "InvalidApiKey", "ASR_AUTHENTICATION"),
        (403, "AccessDenied", "ASR_PERMISSION"),
        (404, "ModelNotFound", "ASR_MODEL_UNAVAILABLE"),
        (429, "insufficient_quota", "ASR_QUOTA"),
        (400, "InvalidParameter", "ASR_REQUEST_REJECTED"),
    ],
)
def test_explicit_rejections_are_known(setup, status, code, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"code": code, "message": "private"})

    adapter = make_adapter(setup, handler)
    with pytest.raises(KnownFailure, match=expected):
        adapter.evaluate(setup[3], setup[4])
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["timeout", "network", "server"])
def test_unknown_never_retries(setup, mode):
    calls = []

    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("private")
        if mode == "network":
            raise httpx.ConnectError("private")
        return httpx.Response(503, json={"code": "InternalError"})

    adapter = make_adapter(setup, handler)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        adapter.evaluate(setup[3], setup[4])
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"output": {"choices": []}},
        body(None),
        {
            "output": {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": [{"text": "partial"}]},
                    }
                ]
            }
        },
    ],
)
def test_malformed_response_is_not_no_text(setup, response):
    adapter = make_adapter(setup, lambda request: httpx.Response(200, json=response))
    with pytest.raises(KnownFailure, match="ASR_INVALID_RESPONSE"):
        adapter.evaluate(setup[3], setup[4])


def test_audio_limit_and_missing_credentials_do_not_call_provider(setup, monkeypatch):
    import app.evaluation.dashscope_asr as module

    calls = []
    adapter = make_adapter(setup, lambda request: calls.append(request))
    monkeypatch.setattr(module, "MAX_DATA_URI_BYTES", 100)
    with pytest.raises(KnownFailure, match="ASR_AUDIO_INCOMPATIBLE"):
        adapter.evaluate(setup[3], setup[4])
    monkeypatch.setattr(module, "MAX_DATA_URI_BYTES", 10_000_000)
    monkeypatch.setattr(module, "MAX_SECONDS", 0.01)
    with pytest.raises(KnownFailure, match="ASR_AUDIO_INCOMPATIBLE"):
        adapter.evaluate(setup[3], setup[4])
    monkeypatch.setattr(module, "MAX_SECONDS", 300)
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    with pytest.raises(KnownFailure, match="ASR_CREDENTIALS_MISSING"):
        adapter.evaluate(setup[3], setup[4])
    assert not calls


def test_successful_response_uses_only_real_usage_fields(setup):
    data = body("words")
    data["usage"] = {
        "seconds": 3,
        "input_tokens_details": {"text_tokens": 2, "other": 8},
        "output_tokens_details": {"text_tokens": 7},
    }
    result = make_adapter(setup, lambda request: httpx.Response(200, json=data)).evaluate(
        setup[3], setup[4]
    )
    assert result.payload["usage"] == {
        "seconds": 3,
        "input_tokens_details": {"text_tokens": 2},
        "output_tokens_details": {"text_tokens": 7},
    }


def test_injected_credential_failure_never_falls_back_to_environment(setup):
    tools, original, derived, inputs, requirement, _ = setup
    calls = []

    def unavailable():
        raise ValueError("private credential error")

    adapter = DashScopeASR(
        original,
        derived,
        tools,
        credential_provider=unavailable,
        client_factory=lambda **kw: calls.append(kw),
    )
    with pytest.raises(KnownFailure, match="ASR_CREDENTIALS_UNAVAILABLE"):
        adapter.evaluate(inputs, requirement)
    assert calls == []
