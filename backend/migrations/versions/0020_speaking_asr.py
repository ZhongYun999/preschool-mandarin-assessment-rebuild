"""Persist Speaking process ASR operations.

Revision ID: 0020_speaking_asr
Revises: 0019_speaking_segments
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020_speaking_asr"
down_revision = "0019_speaking_segments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "speaking_asr_operations",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "segment_id", sa.Uuid(), sa.ForeignKey("speaking_segments.segment_id"), nullable=False
        ),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column(
            "recording_id", sa.Uuid(), sa.ForeignKey("recordings.recording_id"), nullable=False
        ),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("contract", JSONB),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("semantic", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("request_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("retry_request_id", sa.Text()),
        sa.Column("retry_expected_attempts", sa.Integer()),
        sa.UniqueConstraint("segment_id", name="uq_speaking_asr_segment"),
        sa.CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','UNKNOWN')",
            name="ck_speaking_asr_status",
        ),
        sa.CheckConstraint(
            "semantic IS NULL OR semantic IN ('TRANSCRIBED','NO_RECOGNIZED_SPEECH')",
            name="ck_speaking_asr_semantic",
        ),
        sa.CheckConstraint(
            "(status <> 'READY' AND semantic IS NULL AND transcript IS NULL) OR "
            "(status = 'READY' AND semantic IS NOT NULL AND "
            "((semantic = 'TRANSCRIBED' AND transcript IS NOT NULL AND length(trim(transcript)) > 0) "
            "OR (semantic = 'NO_RECOGNIZED_SPEECH' AND transcript IS NULL)))",
            name="ck_speaking_asr_result",
        ),
    )
    op.execute("""INSERT INTO speaking_asr_operations
        (operation_id, segment_id, attempt_id, recording_id, sha256, status, attempts)
        SELECT gen_random_uuid(), s.segment_id, s.attempt_id, r.recording_id, r.sha256, 'WAITING', 0
        FROM speaking_segments s JOIN recordings r ON r.recording_id=s.recording_id
        WHERE r.status='SAVED' AND r.attempt_id=s.attempt_id""")
    op.execute("""CREATE FUNCTION guard_speaking_asr() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'speaking ASR facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM speaking_segments s JOIN recordings r ON r.recording_id=s.recording_id
          WHERE s.segment_id=NEW.segment_id AND s.attempt_id=NEW.attempt_id
          AND r.recording_id=NEW.recording_id AND r.sha256=NEW.sha256 AND r.status='SAVED') THEN
        RAISE EXCEPTION 'speaking ASR input identity mismatch'; END IF;
      IF TG_OP='UPDATE' THEN
        IF (OLD.operation_id, OLD.segment_id, OLD.attempt_id, OLD.recording_id, OLD.sha256)
           IS DISTINCT FROM (NEW.operation_id, NEW.segment_id, NEW.attempt_id, NEW.recording_id, NEW.sha256)
           OR NEW.attempts < OLD.attempts
           OR (OLD.contract IS NOT NULL AND OLD.contract IS DISTINCT FROM NEW.contract)
           OR (OLD.status='READY' AND OLD IS DISTINCT FROM NEW) THEN
          RAISE EXCEPTION 'speaking ASR confirmed identity is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_speaking_asr BEFORE INSERT OR UPDATE OR DELETE ON speaking_asr_operations FOR EACH ROW EXECUTE FUNCTION guard_speaking_asr()"
    )


def downgrade():
    op.execute("LOCK TABLE speaking_asr_operations IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_asr_operations) THEN RAISE EXCEPTION 'speaking ASR facts must be preserved'; END IF; END $$"
    )
    op.drop_table("speaking_asr_operations")
    op.execute("DROP FUNCTION guard_speaking_asr()")
