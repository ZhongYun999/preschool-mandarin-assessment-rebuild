"""Regression coverage for regional credential binding and 0026 migration."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.evaluation import provider_config as service
from app.evaluation.provider_models import ProviderAudit, ProviderConfig, ProviderCredential

pytest_plugins = ("test_qa_formal_text_contract",)


@pytest.fixture
def master(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("VNEXT_LLM_PROVIDER_ENCRYPTION_KEY", key.decode())
    return key


def test_new_credentials_bind_region_and_reuse_is_constrained(db, master):
    with Session(db) as s, s.begin():
        first = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="first",
            api_key="fake-beijing",
        )
        identity = first.credential_id
        row = s.get(ProviderCredential, identity)
        assert row.region == "cn-beijing"
        decoded = json.loads(Fernet(master).decrypt(row.ciphertext))
        assert decoded["region"] == "cn-beijing"
        config = service.save(
            s,
            provider="dashscope",
            model="qwen-plus",
            region="cn-beijing",
            workspace="second",
            credential_id=identity,
        )
        assert config.credential_id == identity
        with pytest.raises(service.CredentialUnavailable):
            service.save(
                s,
                provider="dashscope",
                model="qwen-plus",
                region="ap-southeast-1",
                workspace="first",
                credential_id=identity,
            )
    with Session(db) as s, s.begin():
        second = service.save(
            s, provider="deepseek", model="deepseek-flash", api_key="fake-deepseek"
        )
        assert s.get(ProviderCredential, second.credential_id).region is None
        again = service.save(
            s, provider="deepseek", model="deepseek-v4-pro", credential_id=second.credential_id
        )
        assert again.credential_id == second.credential_id
    with Session(db) as s, s.begin():
        # Test the resolve path independently of the management API, using a deliberately
        # inconsistent but historically representable configuration.
        s.add(
            ProviderConfig(
                config_id=uuid4(),
                revision=100,
                provider="dashscope",
                model="qwen-plus",
                region="us-east-1",
                workspace="test",
                credential_id=identity,
            )
        )
    with Session(db) as s, s.begin(), pytest.raises(service.CredentialUnavailable):
        service.resolve(s)


def test_management_api_reports_region_without_secrets(db, client, master):
    from test_teacher_api import AUTH

    path = "/teacher/llm-provider"
    for region in ("cn-beijing", "ap-southeast-1", "us-east-1"):
        response = client.put(
            path,
            headers=AUTH,
            json={
                "provider": "dashscope",
                "model": "qwen-plus",
                "region": region,
                "workspace": "test",
                "api_key": "fake-only-key",
            },
        )
        assert response.status_code == 200
    response = client.put(
        path,
        headers=AUTH,
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": "fake-only-key"},
    )
    assert response.status_code == 200
    listing = client.get(path + "/credentials", headers=AUTH)
    assert {item["region"] for item in listing.json()} == {
        "cn-beijing",
        "ap-southeast-1",
        "us-east-1",
        None,
    }
    assert "fake-only-key" not in listing.text and "ciphertext" not in listing.text


@pytest.mark.db_isolated
def test_legacy_region_backfill_preserves_ciphertext_and_history(db, master):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    ids = [uuid4() for _ in range(4)]
    providers = ["dashscope", "dashscope", "dashscope", "deepseek"]
    secrets = [
        Fernet(master).encrypt(
            json.dumps(
                {
                    "id": str(identity),
                    "provider": provider,
                    "version": i + 1,
                    "key": "legacy-fixture",
                }
            ).encode()
        )
        for i, (identity, provider) in enumerate(zip(ids, providers, strict=True))
    ]
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0025_llm_provider_config")
        c.commit()
        for i, (identity, provider, ciphertext) in enumerate(
            zip(ids, providers, secrets, strict=True)
        ):
            c.execute(
                text(
                    "INSERT INTO llm_provider_credentials (credential_id,provider,version,ciphertext) VALUES (:id,:provider,:version,:ciphertext)"
                ),
                {"id": identity, "provider": provider, "version": i + 1, "ciphertext": ciphertext},
            )
        records = [
            (ids[0], "dashscope", "cn-beijing"),
            (ids[1], "dashscope", "cn-beijing"),
            (ids[1], "dashscope", "us-east-1"),
            (ids[3], "deepseek", None),
        ]
        for index, (identity, provider, region) in enumerate(records, 1):
            cid = uuid4()
            c.execute(
                text(
                    "INSERT INTO llm_provider_configs (config_id,revision,provider,model,region,workspace,credential_id) VALUES (:id,:rev,:provider,:model,:region,:workspace,:credential)"
                ),
                {
                    "id": cid,
                    "rev": index,
                    "provider": provider,
                    "model": "qwen-plus" if provider == "dashscope" else "deepseek-flash",
                    "region": region,
                    "workspace": "test" if region else None,
                    "credential": identity,
                },
            )
            c.execute(
                text(
                    "INSERT INTO llm_provider_audit (audit_id,action,config_id,credential_id) VALUES (:id,'ACTIVATE_CONFIG',:config,:credential)"
                ),
                {"id": uuid4(), "config": cid, "credential": identity},
            )
        c.commit()
        command.upgrade(config, "head")
        c.commit()
        rows = c.execute(
            text("SELECT credential_id,region,ciphertext FROM llm_provider_credentials")
        ).all()
        by_id = {row.credential_id: (row.region, bytes(row.ciphertext)) for row in rows}
        assert [by_id[i][0] for i in ids] == ["cn-beijing", None, None, None]
        assert [by_id[i][1] for i in ids] == secrets
        assert c.scalar(text("SELECT count(*) FROM llm_provider_configs")) == 4
        assert c.scalar(text("SELECT count(*) FROM llm_provider_audit")) == 4
        c.commit()
        with pytest.raises(DBAPIError, match="credential identity is immutable"):
            c.execute(
                text(
                    "UPDATE llm_provider_credentials SET region='us-east-1' WHERE credential_id=:id"
                ),
                {"id": ids[0]},
            )
        c.rollback()
        with pytest.raises(DBAPIError, match="cannot discard credential region history"):
            command.downgrade(config, "0025_llm_provider_config")
        c.rollback()
    with Session(db) as s:
        for identity in ids[:3]:
            with pytest.raises(service.CredentialUnavailable):
                service._key(s.get(ProviderCredential, identity))
        assert service._key(s.get(ProviderCredential, ids[3])) == "legacy-fixture"
        assert len(list(s.scalars(select(ProviderAudit)))) == 4


@pytest.mark.parametrize("region", ["cn-beijing", "ap-southeast-1", "us-east-1"])
def test_region_ciphertext_tampering_fails_closed(region, master):
    from types import SimpleNamespace

    identity = uuid4()
    value = {
        "id": str(identity),
        "provider": "dashscope",
        "version": 1,
        "region": "other-region",
        "key": "synthetic",
    }
    credential = SimpleNamespace(
        credential_id=identity,
        provider="dashscope",
        version=1,
        region=region,
        enabled=True,
        ciphertext=Fernet(master).encrypt(json.dumps(value).encode()),
    )
    with pytest.raises(service.CredentialUnavailable):
        service._key(credential)


@pytest.mark.db_isolated
def test_upgrade_preserves_existing_evaluation_snapshot(db, qa_formal):
    from test_asr_dependency import claim
    from test_qa_formal_text_contract import FakeText

    from app.evaluation import qa_formal_text_contract as business
    from app.evaluation.models import EvaluationJob, RawEvaluation
    from app.evaluation.worker import execute_claim

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with db.connect() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0025_llm_provider_config")
        c.commit()
    f, _, _, sources = qa_formal()
    assert execute_claim(db, claim(db, f, "dashscope-qwen3-asr", sources))

    class HistoricalAdapter(FakeText):
        def prepare_execution(self, session, now):
            self.snapshot = {"provider": "deepseek", "config_revision": 1, "credential_version": 1}
            return self.snapshot, self

    sources[(business.SOURCE, business.VERSION)] = HistoricalAdapter()
    assert execute_claim(db, claim(db, f, business.SOURCE, sources))
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        raw = s.scalar(select(RawEvaluation).where(RawEvaluation.job_id == job.job_id))
        before = (
            raw.evaluation_id,
            raw.status,
            raw.input_fingerprint,
            raw.input_refs,
            raw.execution_snapshot,
            raw.payload,
        )
        snapshot = job.execution_snapshot
    with db.connect() as c:
        config.attributes["connection"] = c
        command.upgrade(config, "head")
        c.commit()
    with Session(db) as s:
        job = s.get(EvaluationJob, f["jobs"][business.SOURCE])
        raw = s.get(RawEvaluation, before[0])
        assert (
            raw.evaluation_id,
            raw.status,
            raw.input_fingerprint,
            raw.input_refs,
            raw.execution_snapshot,
            raw.payload,
        ) == before
        assert job.execution_snapshot == snapshot
