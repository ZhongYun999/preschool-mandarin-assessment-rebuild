"""Session-scoped access credentials; bearer secrets are never persisted."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SessionCredential(Base):
    __tablename__ = "session_credentials"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_session_credential_digest"),
        CheckConstraint("octet_length(token_digest) = 32", name="ck_session_credential_digest"),
    )
    credential_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("assessment_sessions.session_id"), nullable=False, index=True
    )
    token_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
