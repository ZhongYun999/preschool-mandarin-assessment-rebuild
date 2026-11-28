"""Local paid-call consent, distinct from provider credentials and evaluation facts."""

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, func, text

metadata = MetaData()
authorizations = Table(
    "dev_qa_paid_authorizations_v2",
    metadata,
    Column("submission_id", String(36), primary_key=True),
    Column("source", String, primary_key=True),
    Column("available", Boolean, nullable=False, server_default=text("true")),
    Column("authorized_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def initialize(engine):
    metadata.create_all(engine)
