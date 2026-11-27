"""Transactional global LLM configuration; caller owns the database transaction."""

import json
import os
import re
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.evaluation.provider_models import ProviderAudit, ProviderConfig, ProviderCredential


class CredentialUnavailable(ValueError):
    """Credential is missing, disabled or cannot be safely decrypted."""


def _cipher():
    try:
        return Fernet(os.environ["VNEXT_LLM_PROVIDER_ENCRYPTION_KEY"].encode("ascii"))
    except (KeyError, ValueError, UnicodeError, TypeError):
        raise CredentialUnavailable("Credential encryption unavailable") from None


def _lock(session):
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('vnext_llm_provider_config'))"))


def _key(credential):
    if credential is None or not credential.enabled:
        raise CredentialUnavailable("Credential unavailable")
    try:
        data = json.loads(_cipher().decrypt(credential.ciphertext))
        if (
            (data["id"], data["provider"], data["version"])
            != (str(credential.credential_id), credential.provider, credential.version)
            or (
                credential.provider == "dashscope"
                and (
                    credential.region is None
                    or "region" not in data
                    or data["region"] != credential.region
                )
            )
            or (
                credential.provider == "deepseek"
                and (
                    credential.region is not None
                    or ("region" in data and data["region"] is not None)
                )
            )
            or not isinstance(data["key"], str)
            or not data["key"].strip()
        ):
            raise ValueError
        return data["key"]
    except (InvalidToken, UnicodeError, ValueError, KeyError, TypeError):
        raise CredentialUnavailable("Credential unavailable") from None


def valid_config(provider, model, region, workspace):
    if provider == "dashscope":
        return (
            model in {"qwen-plus", "qwen-turbo"}
            and region in {"cn-beijing", "ap-southeast-1", "us-east-1"}
            and isinstance(workspace, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", workspace) is not None
        )
    return (
        provider == "deepseek"
        and model in {"deepseek-flash", "deepseek-v4-pro"}
        and region is None
        and workspace is None
    )


def save(
    session: Session,
    *,
    provider,
    model,
    region=None,
    workspace=None,
    api_key=None,
    credential_id=None,
):
    if not valid_config(provider, model, region, workspace):
        raise ValueError("Invalid provider configuration")
    if (api_key is None) == (credential_id is None):
        raise ValueError("Provide exactly one credential")
    _lock(session)
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ValueError("Invalid credential")
        version = (
            session.scalar(
                select(func.max(ProviderCredential.version)).where(
                    ProviderCredential.provider == provider
                )
            )
            or 0
        ) + 1
        identity = uuid4()
        value = json.dumps(
            {
                "id": str(identity),
                "provider": provider,
                "version": version,
                "region": region,
                "key": api_key,
            },
            separators=(",", ":"),
        ).encode()
        credential = ProviderCredential(
            credential_id=identity,
            provider=provider,
            version=version,
            region=region,
            ciphertext=_cipher().encrypt(value),
            enabled=True,
        )
        session.add(credential)
        session.flush()
        session.add(ProviderAudit(action="CREATE_CREDENTIAL", credential_id=identity))
    else:
        credential = session.get(ProviderCredential, UUID(str(credential_id)))
        if credential is None or credential.provider != provider or credential.region != region:
            raise CredentialUnavailable("Credential unavailable")
        _key(credential)
    revision = (session.scalar(select(func.max(ProviderConfig.revision))) or 0) + 1
    config = ProviderConfig(
        config_id=uuid4(),
        revision=revision,
        provider=provider,
        model=model,
        region=region,
        workspace=workspace,
        credential_id=credential.credential_id,
    )
    session.add(config)
    session.flush()
    session.add(
        ProviderAudit(
            action="ACTIVATE_CONFIG",
            config_id=config.config_id,
            credential_id=credential.credential_id,
        )
    )
    return config


def disable(session: Session, credential_id):
    _lock(session)
    credential = session.get(ProviderCredential, credential_id)
    if credential is None or not credential.enabled:
        raise CredentialUnavailable("Credential unavailable")
    credential.enabled = False
    session.add(ProviderAudit(action="DISABLE_CREDENTIAL", credential_id=credential_id))


def current(session: Session):
    return session.scalar(select(ProviderConfig).order_by(ProviderConfig.revision.desc()).limit(1))


def resolve(session: Session):
    """Resolve one atomic configuration while holding the shared transaction lock."""
    _lock(session)
    config = current(session)
    if config is None or not valid_config(
        config.provider, config.model, config.region, config.workspace
    ):
        raise CredentialUnavailable("Provider configuration unavailable")
    credential = session.get(ProviderCredential, config.credential_id)
    if (
        credential is None
        or credential.provider != config.provider
        or credential.region != config.region
    ):
        raise CredentialUnavailable("Credential unavailable")
    return config, credential, _key(credential)
