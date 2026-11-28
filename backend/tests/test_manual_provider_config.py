"""Local-only provider configuration confidentiality and identity checks."""

import importlib.util
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

spec = importlib.util.spec_from_file_location(
    "provider_config", Path(__file__).parent / "manual" / "provider_config.py"
)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


@pytest.mark.db_isolated
def test_credentials_encrypt_and_pin_identity(db):
    config.initialize(db)
    config.initialize(db)
    inspector = inspect(db)
    for table in config.metadata.sorted_tables:
        assert {c["name"] for c in inspector.get_columns(table.name)} == {
            c.name for c in table.columns
        }
    with Session(db) as s:
        assert s.scalars(select(config.versions.c.version)).all() == [1]
    key = Fernet.generate_key().decode()
    value = "not-a-real-api-key-unique-test"
    with Session(db) as s, s.begin():
        cid = config.save(s, key, region="cn-beijing", workspace="test-workspace", api_key=value)
        state = config.status(s, key)
        credential_id = state["credential_id"]
        assert state["config_id"] == cid
        assert value not in str(state)
        raw = s.execute(select(config.credentials)).mappings().one()
        assert value.encode() not in raw["encrypted_key"]
        assert config.resolve(s, key, credential_id, "dashscope") == value
        with pytest.raises(config.CredentialUnavailable):
            config.resolve(s, Fernet.generate_key().decode(), credential_id, "dashscope")
        with pytest.raises(config.CredentialUnavailable):
            config.resolve(s, key, credential_id, "another-provider")
        config.save(
            s, key, region="us-east-1", workspace="other", retain_credential_id=credential_id
        )
        assert config.resolve(s, key, credential_id, "dashscope") == value
        config.disable(s, credential_id)
        with pytest.raises(config.CredentialUnavailable):
            config.resolve(s, key, credential_id, "dashscope")
        assert not config.status(s, key)["configured"]
    with Session(db) as s, s.begin():
        assert value not in str(s.execute(select(config.audit)).all())
        s.execute(config.credentials.update().values(encrypted_key=b"broken", enabled=True))
        with pytest.raises(config.CredentialUnavailable):
            config.resolve(s, key, credential_id, "dashscope")


@pytest.mark.db_isolated
def test_local_provider_http_does_not_expose_secrets(db, tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "workbench_provider_http", Path(__file__).parent / "manual" / "workbench.py"
    )
    workbench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workbench)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VNEXT_PROVIDER_CREDENTIAL_KEY", key)
    secret = "fake-key-not-for-use"
    app = workbench.create_app(db, tmp_path)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 1),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as client:
        body = {
            "region": "cn-beijing",
            "workspace": "test-workspace",
            "api_key": secret,
            "retain_credential_id": None,
        }
        response = client.post("/dev/qa/provider-config", json=body)
        assert response.status_code == 200
        assert secret not in response.text and key not in response.text
        state = client.get("/dev/qa/provider-config")
        assert state.json()["configured"]
        assert secret not in state.text and key not in state.text and "encrypted" not in state.text
        credential_id = state.json()["credential_id"]
        changed = {
            "region": "us-east-1",
            "workspace": "next-workspace",
            "api_key": None,
            "retain_credential_id": credential_id,
        }
        assert client.post("/dev/qa/provider-config", json=changed).status_code == 200
        assert client.get("/dev/qa/provider-config").json()["region"] == "us-east-1"
        wrong_app = workbench.create_app(db, tmp_path, master_key=Fernet.generate_key().decode())
        with TestClient(
            wrong_app,
            base_url="http://127.0.0.1:8000",
            client=("127.0.0.1", 1),
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as wrong_client:
            wrong_status = wrong_client.get("/dev/qa/provider-config")
            assert not wrong_status.json()["configured"]
            assert secret not in wrong_status.text and key not in wrong_status.text
        assert (
            client.post(f"/dev/qa/provider-config/{credential_id}/disable").json()["status"]
            == "disabled"
        )
        assert not client.get("/dev/qa/provider-config").json()["configured"]
        assert secret not in caplog.text and key not in caplog.text
