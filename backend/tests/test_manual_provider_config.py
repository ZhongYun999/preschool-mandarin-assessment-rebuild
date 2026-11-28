"""Formal provider configuration used by the local workbench."""

import importlib.util
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_manual_workbench import login_admin, prepare_login

from app.evaluation import provider_config as config
from app.evaluation.provider_models import ProviderAudit, ProviderCredential

spec = importlib.util.spec_from_file_location(
    "workbench_provider_http", Path(__file__).parent / "manual" / "workbench.py"
)
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


def test_credentials_encrypt_and_pin_identity(db, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", key)
    value = "not-a-real-api-key-unique-test"
    with Session(db) as session, session.begin():
        saved = config.save(
            session,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="test-workspace",
            api_key=value,
        )
        credential_id = saved.credential_id
        raw = session.get(ProviderCredential, credential_id)
        assert value.encode() not in raw.ciphertext
        assert config._key(raw) == value
        with pytest.raises(config.CredentialUnavailable):
            config.save(
                session,
                provider="dashscope",
                model="qwen-plus",
                region="us-east-1",
                workspace="other",
                credential_id=credential_id,
            )
        config.disable(session, credential_id)
        with pytest.raises(config.CredentialUnavailable):
            config._key(raw)
    with Session(db) as session:
        assert value not in str(session.scalars(select(ProviderAudit)).all())


@pytest.mark.db_isolated
def test_local_provider_http_does_not_expose_secrets(db, tmp_path, monkeypatch, caplog):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", key)
    prepare_login(db, monkeypatch)
    secret = "fake-key-not-for-use"
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        assert client.get("/teacher/llm-provider").status_code == 401
        login_admin(client)
        body = {
            "provider": "dashscope",
            "model": "qwen-plus",
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "api_key": secret,
        }
        response = client.put("/teacher/llm-provider", json=body)
        assert response.status_code == 200
        assert secret not in response.text and key not in response.text
        state = client.get("/teacher/llm-provider")
        assert state.json()["configured"] and state.json()["available"]
        assert secret not in state.text and key not in state.text and "encrypted" not in state.text
        credential_id = state.json()["credential_id"]
        changed = {**body, "region": "us-east-1", "api_key": None, "credential_id": credential_id}
        assert client.put("/teacher/llm-provider", json=changed).status_code == 422
        assert client.post(f"/teacher/llm-provider/credentials/{credential_id}/disable").json()[
            "disabled"
        ]
        assert not client.get("/teacher/llm-provider").json()["available"]
        assert secret not in caplog.text and key not in caplog.text
