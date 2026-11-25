"""Persist formal Speaking ASR segment operations and execution attempts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_formal_speaking_asr"
down_revision = "0023_speaking_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "speaking_formal_asr_operations",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("segment_id", sa.Uuid(), nullable=False),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("audio_input", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="WAITING"),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB(none_as_null=True)),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.job_id", "evaluation_jobs.submission_id"],
            name="fk_formal_asr_job_submission",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["speaking_segments.segment_id"], name="fk_formal_asr_segment"
        ),
        sa.ForeignKeyConstraint(
            ["recording_id"], ["recordings.recording_id"], name="fk_formal_asr_recording"
        ),
        sa.UniqueConstraint("job_id", "segment_id", name="uq_formal_asr_job_segment"),
        sa.UniqueConstraint("job_id", "position", name="uq_formal_asr_job_position"),
        sa.CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN')",
            name="ck_formal_asr_operation_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR (status <> 'RUNNING' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_formal_asr_operation_lease",
        ),
        sa.CheckConstraint(
            "(status = 'READY') = (result IS NOT NULL)", name="ck_formal_asr_operation_result"
        ),
    )
    op.create_table(
        "speaking_formal_asr_attempts",
        sa.Column("execution_id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True)),
        sa.Column("request_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["speaking_formal_asr_operations.operation_id"],
            name="fk_formal_asr_attempt_operation",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','READY','FAILED','INVALID','UNKNOWN','UNDISPATCHED')",
            name="ck_formal_asr_attempt_status",
        ),
    )
    op.execute("""CREATE FUNCTION guard_formal_asr_operation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'formal ASR operations must be preserved'; END IF;
      IF (NEW.operation_id, NEW.job_id, NEW.submission_id, NEW.segment_id, NEW.recording_id,
          NEW.position, NEW.source_sha256, NEW.audio_input, NEW.created_at) IS DISTINCT FROM
         (OLD.operation_id, OLD.job_id, OLD.submission_id, OLD.segment_id, OLD.recording_id,
          OLD.position, OLD.source_sha256, OLD.audio_input, OLD.created_at) OR OLD.status = 'READY'
      THEN RAISE EXCEPTION 'formal ASR operation identity and success are immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_formal_asr_operation BEFORE UPDATE OR DELETE ON speaking_formal_asr_operations FOR EACH ROW EXECUTE FUNCTION guard_formal_asr_operation()"
    )
    op.execute("""CREATE FUNCTION guard_formal_asr_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'formal ASR attempts must be preserved'; END IF;
      IF (NEW.execution_id, NEW.operation_id, NEW.created_at) IS DISTINCT FROM
         (OLD.execution_id, OLD.operation_id, OLD.created_at) OR OLD.status <> 'RUNNING'
         OR (OLD.dispatch_started_at IS NOT NULL AND NEW.dispatch_started_at IS DISTINCT FROM OLD.dispatch_started_at)
      THEN RAISE EXCEPTION 'formal ASR attempt history is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_formal_asr_attempt BEFORE UPDATE OR DELETE ON speaking_formal_asr_attempts FOR EACH ROW EXECUTE FUNCTION guard_formal_asr_attempt()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE speaking_formal_asr_attempts, speaking_formal_asr_operations IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_formal_asr_operations)
      OR EXISTS (SELECT 1 FROM speaking_formal_asr_attempts) THEN
      RAISE EXCEPTION 'cannot discard formal ASR history'; END IF; END $$""")
    op.drop_table("speaking_formal_asr_attempts")
    op.drop_table("speaking_formal_asr_operations")
    op.execute("DROP FUNCTION guard_formal_asr_attempt()")
    op.execute("DROP FUNCTION guard_formal_asr_operation()")
