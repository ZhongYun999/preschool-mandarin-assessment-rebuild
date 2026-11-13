"""Add idempotent task-bank import audit."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_import_batches"
down_revision = "0002_taskbank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("bank_id", sa.Uuid(), sa.ForeignKey("task_banks.bank_id"), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("import_batches")
