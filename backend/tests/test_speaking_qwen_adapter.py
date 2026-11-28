"""The Speaking provider adapter is exercised only through a mock transport."""

import base64
import json
from dataclasses import asdict

import dashscope
import httpx
import pytest
from test_dashscope_asr import body

from app.evaluation.speaking_qwen_adapter import SpeakingQwenASR
from app.evaluation.speaking_qwen_contract import valid_full_consumption, valid_parameters
from app.evaluation.worker import KnownFailure
from app.media.audio_normalization import AudioSpec

PARAMS = {
    "region": "cn-beijing",
    "workspace": "test-workspace",
    "enable_itn": False,
    "language": None,
}


def adapter(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SpeakingQwenASR(
        client_factory=lambda **kwargs: dashscope.Client(**kwargs, http_client=http),
        key_provider=lambda: "offline-test-only",
    )


def test_text_and_audio_request():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=body("离线转写"))

    result = adapter(handle).recognize(b"fixture-wav", PARAMS)
    assert result == {"status": "TEXT", "text": "离线转写", "request_id": "request-1"}
    sent = json.loads(calls[0].content)
    assert sent["model"] == "qwen3-asr-flash"
    uri = sent["input"]["messages"][0]["content"][0]["audio"]
    assert base64.b64decode(uri.split(",", 1)[1]) == b"fixture-wav"


@pytest.mark.parametrize("value", ["", " ", None])
def test_empty_is_not_confirmed_no_speech(value):
    with pytest.raises(KnownFailure, match="ASR_INVALID_RESPONSE"):
        adapter(lambda request: httpx.Response(200, json=body(value))).recognize(b"wav", PARAMS)


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "InvalidApiKey", "ASR_AUTHENTICATION"),
        (404, "ModelNotFound", "ASR_MODEL_UNAVAILABLE"),
        (429, "insufficient_quota", "ASR_QUOTA"),
        (400, "InvalidParameter", "ASR_REQUEST_REJECTED"),
    ],
)
def test_classified_failure(status, code, expected):
    with pytest.raises(KnownFailure, match=expected):
        adapter(lambda request: httpx.Response(status, json={"code": code})).recognize(
            b"wav", PARAMS
        )


def test_timeout_is_unknown_and_not_retried():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("offline")

    with pytest.raises(RuntimeError, match="outcome unknown"):
        adapter(handle).recognize(b"wav", PARAMS)
    assert len(calls) == 1


def test_base64_limit_is_checked_before_request(monkeypatch):
    import app.evaluation.speaking_qwen_adapter as provider

    calls = []
    monkeypatch.setattr(provider, "MAX_DATA_URI_BYTES", 30)
    with pytest.raises(KnownFailure, match="ASR_AUDIO_INCOMPATIBLE"):
        adapter(lambda request: calls.append(request)).recognize(b"123456789", PARAMS)
    assert calls == []


def test_provider_failure_preserves_safe_request_id():
    with pytest.raises(KnownFailure) as failure:
        adapter(
            lambda request: httpx.Response(
                400, json={"code": "InvalidParameter", "request_id": "request-123"}
            )
        ).recognize(b"wav", PARAMS)
    assert failure.value.code == "ASR_REQUEST_REJECTED"
    assert failure.value.request_id == "request-123"


def test_full_source_is_required_and_processing_contract_is_frozen():
    params = {
        **PARAMS,
        "provider": "dashscope",
        "model": "qwen3-asr-flash",
        "processing_version": "v1",
        "confirmed_no_text": False,
        "audio_spec": asdict(
            AudioSpec("wav", "pcm_s16le", 16000, 1, "v1", "ffmpeg-v1", "ffprobe-v1")
        ),
    }
    assert valid_parameters(params)
    assert not valid_parameters({**params, "confirmed_no_text": True})
    segments = [
        {
            "segment_id": str(i),
            "recording_id": str(i),
            "position": i,
            "sha256": "a" * 64,
            "storage_ref": "ref",
            "media_type": "audio/wav",
        }
        for i in range(2)
    ]
    source = {"submission_id": "submission", "sha256": "source", "segments": segments}
    audio = [{**segment, "processing_version": "v1"} for segment in segments]
    consumption = {"segment_ids": ["0", "1"], "audio_inputs": audio, "derived_recordings": []}
    payload = {"audio_inputs": audio, "submission_id": "submission", "source_sha256": "source"}
    assert valid_full_consumption(source, consumption, {"parameters": params}, None, payload)
    # A reused WAV may have been uploaded with a different declared MIME type;
    # the consumed identity reports the inspected bytes, while retaining their digest and ref.
    source["segments"][0]["media_type"] = "audio/webm"
    assert valid_full_consumption(source, consumption, {"parameters": params}, None, payload)
    assert not valid_full_consumption(
        source,
        {**consumption, "segment_ids": ["0"], "audio_inputs": audio[:1]},
        {"parameters": params},
        None,
        None,
    )
