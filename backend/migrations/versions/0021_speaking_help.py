"""Persist Speaking help requests and generation facts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_speaking_help"
down_revision = "0020_speaking_asr"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "speaking_help_requests",
        sa.Column("help_request_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("input_frontier", JSONB, nullable=False),
        sa.Column("execution_history", JSONB, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("guidance_text", sa.Text()),
        sa.Column("prompt_sha256", sa.Text()),
        sa.Column("contract", JSONB),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("provider_request_id", sa.Text()),
        sa.Column("retry_request_id", sa.Text()),
        sa.Column("retry_expected_attempts", sa.Integer()),
        sa.UniqueConstraint("attempt_id", "request_id", name="uq_speaking_help_request"),
        sa.UniqueConstraint("attempt_id", "position", name="uq_speaking_help_position"),
        sa.CheckConstraint("position > 0", name="ck_speaking_help_position"),
        sa.CheckConstraint("source = 'child_requested'", name="ck_speaking_help_source"),
        sa.CheckConstraint(
            "jsonb_typeof(execution_history) = 'array'", name="ck_speaking_help_history"
        ),
        sa.CheckConstraint(
            "status IN ('WAITING','RUNNING','FAILED','UNKNOWN','READY','DELIVERED','VOIDED')",
            name="ck_speaking_help_status",
        ),
        sa.CheckConstraint(
            "(status IN ('READY','DELIVERED') AND guidance_text IS NOT NULL AND length(trim(guidance_text)) > 0) OR "
            "(status = 'VOIDED' AND (guidance_text IS NULL OR length(trim(guidance_text)) > 0)) OR "
            "(status NOT IN ('READY','DELIVERED','VOIDED') AND guidance_text IS NULL)",
            name="ck_speaking_help_text",
        ),
    )
    op.create_index(
        "uq_speaking_help_active",
        "speaking_help_requests",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('DELIVERED','VOIDED')"),
    )
    op.execute("""CREATE FUNCTION guard_speaking_help() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'speaking help facts must be preserved'; END IF;
      IF TG_OP='UPDATE' THEN
        IF (OLD.help_request_id,OLD.attempt_id,OLD.request_id,OLD.position,OLD.source,OLD.input_frontier)
          IS DISTINCT FROM (NEW.help_request_id,NEW.attempt_id,NEW.request_id,NEW.position,NEW.source,NEW.input_frontier)
          OR NEW.attempts < OLD.attempts
          OR (NEW.status='DELIVERED' AND OLD.status NOT IN ('READY','DELIVERED'))
          OR (OLD.guidance_text IS NOT NULL AND OLD.guidance_text IS DISTINCT FROM NEW.guidance_text)
          OR (SELECT COALESCE(jsonb_agg(value ORDER BY ord), '[]'::jsonb)
              FROM jsonb_array_elements(NEW.execution_history) WITH ORDINALITY AS e(value,ord)
              WHERE ord <= jsonb_array_length(OLD.execution_history)) IS DISTINCT FROM OLD.execution_history
          OR (OLD.status IN ('DELIVERED','VOIDED') AND OLD IS DISTINCT FROM NEW)
          OR (OLD.status='READY' AND (
            NEW.status NOT IN ('READY','DELIVERED','VOIDED')
            OR (to_jsonb(OLD) - ARRAY['status','error_code','execution_history'])
               IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','error_code','execution_history'])
            OR (NEW.status <> 'VOIDED' AND OLD.error_code IS DISTINCT FROM NEW.error_code)
            OR (NEW.status='READY' AND OLD IS DISTINCT FROM NEW)
          )) THEN RAISE EXCEPTION 'speaking help confirmed facts are immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_speaking_help BEFORE UPDATE OR DELETE ON speaking_help_requests FOR EACH ROW EXECUTE FUNCTION guard_speaking_help()"
    )


def downgrade():
    op.execute("LOCK TABLE speaking_help_requests IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_help_requests) THEN RAISE EXCEPTION 'speaking help facts must be preserved'; END IF; END $$"
    )
    op.drop_table("speaking_help_requests")
    op.execute("DROP FUNCTION guard_speaking_help()")
