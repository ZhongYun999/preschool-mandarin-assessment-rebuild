"""Persist the committed result for an idempotent task-bank import."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ImportBatch(Base):
    __tablename__ = "import_batches"
    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(Uuid, unique=True, nullable=False, default=uuid4)
    bank_id: Mapped[UUID] = mapped_column(ForeignKey("task_banks.bank_id"), nullable=False)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
