from types import SimpleNamespace
from uuid import uuid4

import pytest
from dashscope import errors
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_fixed_audio import Synth, published, wav

from app.dashscope_tts import DashScopeSynthesizer, TTSError
from app.fixed_audio import AudioStore, FixedAudio, validate_config
from app.main import app
from app.teacher_api import get_audio_store, get_db
from app.tts_attempt import TTSAttempt
from app.tts_worker import authorize_retry, run_audio

CONFIG = {
    "provider": "dashscope",
    "model": "cosyvoice-v2",
    "voice": "voice-test",
    "version": "cosyvoice-sdk-v2",
    "speed": 1.25,
    "format": "wav",
    "sample_rate": 8000,
}
AUTH = {"Authorization": "Bearer test-token"}


def test_sdk_mapping_and_audio_validation(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-example-key")
    calls = {}

    def synthesize(source, **kwargs):
        calls["source"], calls["kwargs"] = source, kwargs
        return wav()

    def client_factory(**kwargs):
        calls["client"] = kwargs
        return SimpleNamespace(audio=SimpleNamespace(tts=SimpleNamespace(synthesize=synthesize)))

    adapter = DashScopeSynthesizer(client_factory)
    result = adapter.synthesize("完整文本? 另一句。", CONFIG)
    assert result.data == wav() and result.media_type == "audio/wav"
    assert calls["source"] == "完整文本? 另一句。"
    assert calls["kwargs"] == {
        "model": "cosyvoice-v2",
        "voice": "voice-test",
        "format": "wav",
        "sample_rate": 8000,
        "rate": 1.25,
    }
    assert calls["client"]["max_retries"] == 0
    with pytest.raises(TTSError, match="invalid_config"):
        adapter.synthesize("text", {**CONFIG, "pitch": 5})
    with pytest.raises(ValueError, match="Credentials"):
        validate_config({**CONFIG, "api_key": "private-example-key"})
    adapter = DashScopeSynthesizer(
        lambda **kw: SimpleNamespace(
            audio=SimpleNamespace(tts=SimpleNamespace(synthesize=lambda *a, **k: b"ID3notwav"))
        )
    )
    with pytest.raises(TTSError, match="invalid_audio"):
        adapter.synthesize("text", CONFIG)


@pytest.mark.parametrize(
    "exception,category,uncertain",
    [
        (errors.APITimeoutError("secret"), "timeout", True),
        (errors.APIConnectionError("secret"), "network", True),
        (errors.AuthenticationError("secret"), "authentication", False),
        (errors.RateLimitError("secret", code="insufficient_quota"), "quota", False),
        (errors.BadRequestError("secret"), "invalid_config", False),
    ],
)
def test_sdk_errors_are_classified_without_secrets(monkeypatch, exception, category, uncertain):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")

    def fail(*args, **kwargs):
        raise exception

    adapter = DashScopeSynthesizer(
        lambda **kw: SimpleNamespace(audio=SimpleNamespace(tts=SimpleNamespace(synthesize=fail)))
    )
    with pytest.raises(TTSError) as captured:
        adapter.synthesize("text", CONFIG)
    assert captured.value.category == category and captured.value.uncertain == uncertain
    assert "secret" not in str(captured.value)


def test_missing_key_fails_without_call(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(TTSError, match="missing_credentials"):
        DashScopeSynthesizer().synthesize("text", CONFIG)


def test_interrupted_call_is_unknown_and_manual_retry_only(db, bank, tmp_path):
    published(db, bank)
    with Session(db) as s, s.begin():
        audio = s.scalar(select(FixedAudio).order_by(FixedAudio.audio_id))
        audio_id = audio.audio_id
        s.add(TTSAttempt(attempt_id=uuid4(), audio_id=audio_id, state="SENDING"))
    synth, store = Synth(), AudioStore(tmp_path)
    assert run_audio(db, audio_id, store, synth) == "unknown"
    for _ in range(3):
        assert run_audio(db, audio_id, store, synth) == "unknown"
    assert synth.calls == []
    with pytest.raises(ValueError, match="acknowledgement"), Session(db) as s, s.begin():
        authorize_retry(s, audio_id)
    with Session(db) as s, s.begin():
        authorize_retry(s, audio_id, accept_unknown=True)
    assert run_audio(db, audio_id, store, synth) == "ready"
    assert run_audio(db, audio_id, store, synth) == "ready"
    assert len(synth.calls) == 1


def test_saved_result_recovers_without_provider_call(db, bank, tmp_path):
    published(db, bank)
    store, synth = AudioStore(tmp_path), Synth()
    ref, digest = store.put(wav())
    with Session(db) as s, s.begin():
        audio_id = s.scalar(select(FixedAudio.audio_id))
        s.add(
            TTSAttempt(
                audio_id=audio_id,
                state="SAVED",
                storage_ref=ref,
                content_sha256=digest,
                media_type="audio/wav",
            )
        )
    assert run_audio(db, audio_id, store, synth) == "ready"
    assert synth.calls == []
    with Session(db) as s:
        assert s.get(FixedAudio, audio_id).storage_ref == ref


def test_teacher_status_trigger_retry_and_preview(db, bank, tmp_path, monkeypatch):
    paper_id, *_ = published(db, bank)
    store = AudioStore(tmp_path)
    with Session(db) as s, s.begin():
        audio_id = s.scalar(select(FixedAudio.audio_id))
        s.add(TTSAttempt(audio_id=audio_id, state="SENDING"))
    run_audio(db, audio_id, store, Synth())
    monkeypatch.setenv("TEACHER_MANAGEMENT_TOKEN", "test-token")
    monkeypatch.setattr("app.teacher_auth.load_dotenv", lambda: None)

    def override_db():
        with Session(db) as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_audio_store] = lambda: store
    try:
        with TestClient(app) as client:
            path = f"/teacher/papers/{paper_id}/audio"
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.post(path + "/prepare").status_code == 401
            assert client.get(f"{path}/{audio_id}/content").status_code == 401
            assert client.post(f"{path}/{audio_id}/retry", json={}).status_code == 401
            report = client.get(path, headers=AUTH)
            assert report.status_code == 200 and not report.json()["ready"]
            assert "storage_ref" not in report.text and str(tmp_path) not in report.text
            assert client.post(path + "/prepare", headers=AUTH).status_code == 202
            assert client.post(f"{path}/{audio_id}/retry", headers=AUTH, json={}).status_code == 409
            assert (
                client.post(
                    f"{path}/{audio_id}/retry", headers=AUTH, json={"accept_unknown": True}
                ).status_code
                == 202
            )
            assert run_audio(db, audio_id, store, Synth()) == "ready"
            response = client.get(f"{path}/{audio_id}/content", headers=AUTH)
            assert response.status_code == 200 and response.content == wav()
            assert response.headers["content-type"] == "audio/wav"
            with Session(db) as s:
                row = s.get(FixedAudio, audio_id)
                file_path = tmp_path / row.storage_ref
                file_path.write_bytes(b"corrupt")
            report = client.get(path, headers=AUTH).json()
            assert any(row["diagnostic"] == "file_corrupt" for row in report["items"])
            file_path.unlink()
            report = client.get(path, headers=AUTH).json()
            assert any(row["diagnostic"] == "file_missing" for row in report["items"])
            assert client.get(f"{path}/{audio_id}/content", headers=AUTH).status_code == 409
            monkeypatch.delenv("TEACHER_MANAGEMENT_TOKEN")
            assert client.get(path, headers=AUTH).status_code == 503
    finally:
        app.dependency_overrides.clear()
