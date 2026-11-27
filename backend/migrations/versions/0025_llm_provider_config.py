"""Persist global LLM configuration and per-execution snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025_llm_provider_config"
down_revision = "0024_formal_speaking_asr"
branch_labels = None
depends_on = None


def upgrade():
    # Earlier migrations can backfill jobs in this same transaction. Flush their
    # deferred consistency triggers before altering evaluation_jobs.
    op.execute("SET CONSTRAINTS check_job_raw, check_raw_job IMMEDIATE")
    op.create_table(
        "llm_provider_credentials",
        sa.Column("credential_id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("provider", "version", name="uq_llm_credential_provider_version"),
        sa.UniqueConstraint(
            "credential_id", "provider", name="uq_llm_credential_identity_provider"
        ),
        sa.CheckConstraint(
            "provider IN ('dashscope','deepseek')", name="ck_llm_credential_provider"
        ),
        sa.CheckConstraint("version > 0", name="ck_llm_credential_version"),
    )
    op.create_table(
        "llm_provider_configs",
        sa.Column("config_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("region", sa.Text()),
        sa.Column("workspace", sa.Text()),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id", "provider"],
            ["llm_provider_credentials.credential_id", "llm_provider_credentials.provider"],
            name="fk_llm_config_credential_provider",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("provider IN ('dashscope','deepseek')", name="ck_llm_config_provider"),
        sa.CheckConstraint("revision > 0", name="ck_llm_config_revision"),
        sa.UniqueConstraint("revision", name="uq_llm_config_revision"),
    )
    op.create_table(
        "llm_provider_audit",
        sa.Column("audit_id", sa.Uuid(), primary_key=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("config_id", sa.Uuid(), sa.ForeignKey("llm_provider_configs.config_id")),
        sa.Column(
            "credential_id", sa.Uuid(), sa.ForeignKey("llm_provider_credentials.credential_id")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("evaluation_jobs", sa.Column("execution_snapshot", JSONB(none_as_null=True)))
    op.add_column("raw_evaluations", sa.Column("execution_snapshot", JSONB(none_as_null=True)))
    op.create_check_constraint(
        "ck_evaluation_job_execution_snapshot",
        "evaluation_jobs",
        "execution_snapshot IS NULL OR jsonb_typeof(execution_snapshot) = 'object'",
    )
    op.create_check_constraint(
        "ck_raw_evaluation_execution_snapshot",
        "raw_evaluations",
        "execution_snapshot IS NULL OR jsonb_typeof(execution_snapshot) = 'object'",
    )
    op.execute("""CREATE FUNCTION guard_llm_credential() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'credential history must be preserved'; END IF;
      IF (NEW.credential_id,NEW.provider,NEW.version,NEW.ciphertext,NEW.created_at) IS DISTINCT FROM
         (OLD.credential_id,OLD.provider,OLD.version,OLD.ciphertext,OLD.created_at)
         OR (NOT OLD.enabled AND NEW.enabled) THEN RAISE EXCEPTION 'credential identity is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_llm_credential BEFORE UPDATE OR DELETE ON llm_provider_credentials FOR EACH ROW EXECUTE FUNCTION guard_llm_credential()"
    )
    op.execute("""CREATE FUNCTION reject_llm_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'LLM configuration history is immutable'; END $$""")
    op.execute("""CREATE FUNCTION guard_llm_job_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.status = 'RUNNING' AND NEW.claim_token IS NOT DISTINCT FROM OLD.claim_token
         AND NEW.execution_snapshot IS DISTINCT FROM OLD.execution_snapshot
      THEN RAISE EXCEPTION 'running execution snapshot is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_llm_job_snapshot BEFORE UPDATE ON evaluation_jobs FOR EACH ROW EXECUTE FUNCTION guard_llm_job_snapshot()"
    )
    op.execute(
        "CREATE TRIGGER guard_llm_config BEFORE UPDATE OR DELETE ON llm_provider_configs FOR EACH ROW EXECUTE FUNCTION reject_llm_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER guard_llm_audit BEFORE UPDATE OR DELETE ON llm_provider_audit FOR EACH ROW EXECUTE FUNCTION reject_llm_history_mutation()"
    )
    op.execute("SET CONSTRAINTS check_job_raw, check_raw_job DEFERRED")


def downgrade():
    op.execute(
        "LOCK TABLE llm_provider_audit, llm_provider_configs, llm_provider_credentials IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM llm_provider_audit) OR EXISTS (SELECT 1 FROM llm_provider_configs) OR EXISTS (SELECT 1 FROM llm_provider_credentials) OR EXISTS (SELECT 1 FROM raw_evaluations WHERE execution_snapshot IS NOT NULL) OR EXISTS (SELECT 1 FROM evaluation_jobs WHERE execution_snapshot IS NOT NULL) THEN RAISE EXCEPTION 'cannot discard LLM provider history'; END IF; END $$"""
    )
    op.execute("DROP TRIGGER guard_llm_job_snapshot ON evaluation_jobs")
    op.execute("DROP FUNCTION guard_llm_job_snapshot()")
    op.drop_constraint("ck_raw_evaluation_execution_snapshot", "raw_evaluations", type_="check")
    op.drop_constraint("ck_evaluation_job_execution_snapshot", "evaluation_jobs", type_="check")
    op.drop_column("raw_evaluations", "execution_snapshot")
    op.drop_column("evaluation_jobs", "execution_snapshot")
    op.drop_table("llm_provider_audit")
    op.drop_table("llm_provider_configs")
    op.drop_table("llm_provider_credentials")
    op.execute("DROP FUNCTION reject_llm_history_mutation()")
    op.execute("DROP FUNCTION guard_llm_credential()")
