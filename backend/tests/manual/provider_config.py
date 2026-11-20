"""Credential storage for the loopback QA workbench only.

Schema is deliberately independent from the production ORM and Alembic history.
"""

import json
import re
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

metadata = MetaData()
versions = Table("dev_qa_schema_version", metadata, Column("version", Integer, primary_key=True))
credentials = Table(
    "dev_qa_credentials",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("provider", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("encrypted_key", LargeBinary, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("provider", "version"),
)
configs = Table(
    "dev_qa_provider_configs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("revision", BigInteger, Identity(), nullable=False, unique=True),
    Column("provider", String, nullable=False),
    Column("credential_id", String(36), ForeignKey("dev_qa_credentials.id"), nullable=False),
    Column("region", String, nullable=False),
    Column("workspace", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
audit = Table(
    "dev_qa_provider_audit",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("action", String, nullable=False),
    Column("config_id", String(36), nullable=True),
    Column("credential_id", String(36), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
paper_configs = Table(
    "dev_qa_paper_configs",
    metadata,
    Column("paper_id", String(36), primary_key=True),
    Column("config_id", String(36), ForeignKey("dev_qa_provider_configs.id"), nullable=False),
    Column("plugin_version", String(36), nullable=False),
    Column("requirements", JSONB, nullable=False),
)
authorizations = Table(
    "dev_qa_call_authorizations",
    metadata,
    Column("submission_id", String(36), primary_key=True),
    Column("asr_consumed", Boolean, nullable=False, server_default=text("false")),
    Column("llm_consumed", Boolean, nullable=False, server_default=text("false")),
    Column("authorized_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class CredentialUnavailable(ValueError):
    """A missing, disabled, tampered or undecryptable credential."""


def initialize(engine):
    """Initialize the independent v1 schema, without touching main Alembic state."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('dev_qa_provider_schema'))"))
        metadata.create_all(conn)
        conn.execute(insert(versions).values(version=1).on_conflict_do_nothing())
        if conn.scalars(select(versions.c.version)).all() != [1]:
            raise RuntimeError("Unsupported local QA provider schema version")


def cipher(master_key):
    try:
        return Fernet(master_key.encode("ascii"))
    except (AttributeError, ValueError, UnicodeError, TypeError):
        raise CredentialUnavailable("Credential encryption unavailable") from None


def _log(db, action, config_id=None, credential_id=None):
    db.execute(
        audit.insert().values(
            id=str(uuid4()), action=action, config_id=config_id, credential_id=credential_id
        )
    )


def save(db, master_key, *, region, workspace, api_key=None, retain_credential_id=None):
    if region not in {"cn-beijing", "ap-southeast-1", "us-east-1"}:
        raise ValueError("Invalid region")
    if not isinstance(workspace, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", workspace
    ):
        raise ValueError("Invalid workspace")
    if bool(api_key) == bool(retain_credential_id):
        raise ValueError("Supply a new key or explicitly retain one credential")
    provider = "dashscope"
    if api_key:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ValueError("Invalid credential")
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('dev_qa_dashscope_credential_version'))")
        )
        version = (
            db.scalar(
                select(func.coalesce(func.max(credentials.c.version), 0)).where(
                    credentials.c.provider == provider
                )
            )
            + 1
        )
        identity = str(uuid4())
        payload = json.dumps(
            {"provider": provider, "id": identity, "version": version, "key": api_key},
            separators=(",", ":"),
        ).encode()
        encrypted = cipher(master_key).encrypt(payload)
        db.execute(
            credentials.insert().values(
                id=identity,
                provider=provider,
                version=version,
                encrypted_key=encrypted,
                enabled=True,
            )
        )
        _log(db, "CREATE_CREDENTIAL", credential_id=identity)
    else:
        identity = str(UUID(str(retain_credential_id)))
        row = db.execute(
            select(credentials.c.provider, credentials.c.enabled).where(
                credentials.c.id == identity
            )
        ).one_or_none()
        if row is None or row.provider != provider or not row.enabled:
            raise CredentialUnavailable("Credential unavailable")
        # Verify that retention does not silently adopt an unreadable key.
        resolve(db, master_key, identity, provider)
    config_id = str(uuid4())
    db.execute(
        configs.insert().values(
            id=config_id,
            provider=provider,
            credential_id=identity,
            region=region,
            workspace=workspace,
        )
    )
    _log(db, "SAVE_CONFIG", config_id, identity)
    return config_id


def resolve(db, master_key, credential_id, provider):
    row = (
        db.execute(select(credentials).where(credentials.c.id == str(credential_id)))
        .mappings()
        .one_or_none()
    )
    if row is None or not row["enabled"] or row["provider"] != provider:
        raise CredentialUnavailable("Credential unavailable")
    try:
        value = json.loads(cipher(master_key).decrypt(row["encrypted_key"]))
        if (value["id"], value["provider"], value["version"]) != (
            row["id"],
            row["provider"],
            row["version"],
        ):
            raise ValueError
        if not isinstance(value["key"], str) or not value["key"].strip():
            raise ValueError
        return value["key"]
    except (InvalidToken, UnicodeError, ValueError, KeyError, TypeError):
        raise CredentialUnavailable("Credential unavailable") from None


def disable(db, credential_id):
    result = db.execute(
        update(credentials)
        .where(credentials.c.id == str(credential_id), credentials.c.enabled.is_(True))
        .values(enabled=False)
    )
    if result.rowcount != 1:
        raise CredentialUnavailable("Credential unavailable")
    _log(db, "DISABLE_CREDENTIAL", credential_id=str(credential_id))


def status(db, master_key):
    row = (
        db.execute(select(configs).order_by(configs.c.revision.desc()).limit(1))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return {"configured": False}
    try:
        resolve(db, master_key, row["credential_id"], row["provider"])
        available = True
    except CredentialUnavailable:
        available = False
    credential_version = db.scalar(
        select(credentials.c.version).where(credentials.c.id == row["credential_id"])
    )
    return {
        "configured": available,
        "provider": row["provider"],
        "config_id": row["id"],
        "credential_id": row["credential_id"],
        "credential_version": credential_version,
        "region": row["region"],
        "workspace": row["workspace"],
        "asr_model": "qwen3-asr-flash",
        "llm_model": "qwen-plus",
    }
