"""Persistent global text-provider configuration and credential identities."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProviderCredential(Base):
    __tablename__ = "llm_provider_credentials"
    __table_args__ = (
        UniqueConstraint("provider", "version", name="uq_llm_credential_provider_version"),
        UniqueConstraint("credential_id", "provider", name="uq_llm_credential_identity_provider"),
        CheckConstraint("provider IN ('dashscope','deepseek')", name="ck_llm_credential_provider"),
        CheckConstraint("version > 0", name="ck_llm_credential_version"),
        CheckConstraint(
            "provider <> 'deepseek' OR region IS NULL", name="ck_llm_credential_region"
        ),
    )
    credential_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str | None] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        CheckConstraint("provider IN ('dashscope','deepseek')", name="ck_llm_config_provider"),
        CheckConstraint("revision > 0", name="ck_llm_config_revision"),
        UniqueConstraint("revision", name="uq_llm_config_revision"),
        ForeignKeyConstraint(
            ["credential_id", "provider"],
            ["llm_provider_credentials.credential_id", "llm_provider_credentials.provider"],
            name="fk_llm_config_credential_provider",
        ),
    )
    config_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(Text)
    workspace: Mapped[str | None] = mapped_column(Text)
    credential_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderAudit(Base):
    __tablename__ = "llm_provider_audit"
    audit_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    config_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_provider_configs.config_id")
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_provider_credentials.credential_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
