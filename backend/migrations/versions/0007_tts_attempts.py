"""Persist technical attempts without duplicating frozen requests."""

import sqlalchemy as sa
from alembic import op

revision = "0007_tts_attempts"
down_revision = "0006_fixed_audio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tts_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "audio_id", sa.Uuid(), sa.ForeignKey("fixed_audio_requests.audio_id"), nullable=False
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error_category", sa.Text()),
        sa.Column("retry_authorized", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("storage_ref", sa.Text()),
        sa.Column("content_sha256", sa.Text()),
        sa.Column("media_type", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint(
            "state IN ('SENDING','SAVED','DONE','FAILED','UNKNOWN')", name="ck_tts_attempt_state"
        ),
    )
    op.create_index("ix_tts_attempts_audio_id", "tts_attempts", ["audio_id"])


def downgrade() -> None:
    op.execute("LOCK TABLE tts_attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM tts_attempts) THEN
      RAISE EXCEPTION 'cannot downgrade: TTS attempts must be preserved'; END IF; END $$""")
    op.drop_index("ix_tts_attempts_audio_id", table_name="tts_attempts")
    op.drop_table("tts_attempts")
