"""Durable evaluation jobs and immutable raw evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_evaluation_jobs"
down_revision = "0011_session_task_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_intent_identity",
        "evaluation_intents",
        ["intent_id", "submission_id", "paper_entry_id", "requirement_index"],
    )
    op.create_table(
        "evaluation_jobs",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("paper_entry_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_index", sa.Integer(), nullable=False),
        sa.Column("requirement", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="WAITING"),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("input_refs", JSONB()),
        sa.Column("input_fingerprint", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("intent_id", name="uq_evaluation_job_intent"),
        sa.UniqueConstraint("job_id", "submission_id", name="uq_evaluation_job_submission"),
        sa.ForeignKeyConstraint(
            ["intent_id", "submission_id", "paper_entry_id", "requirement_index"],
            [
                "evaluation_intents.intent_id",
                "evaluation_intents.submission_id",
                "evaluation_intents.paper_entry_id",
                "evaluation_intents.requirement_index",
            ],
            name="fk_job_intent_identity",
        ),
        sa.CheckConstraint(
            "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN')",
            name="ck_evaluation_job_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR (status <> 'RUNNING' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_evaluation_job_lease",
        ),
        sa.CheckConstraint(
            "(input_refs IS NULL) = (input_fingerprint IS NULL)",
            name="ck_evaluation_job_input_pair",
        ),
        sa.CheckConstraint(
            "status = 'WAITING' OR input_fingerprint IS NOT NULL",
            name="ck_evaluation_job_claim_input",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(requirement) = 'object'", name="ck_evaluation_job_requirement"
        ),
        sa.CheckConstraint(
            "input_refs IS NULL OR jsonb_typeof(input_refs) = 'object'",
            name="ck_evaluation_job_input_refs",
        ),
    )
    op.create_table(
        "raw_evaluations",
        sa.Column("evaluation_id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_refs", JSONB(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("engine_version", sa.Text()),
        sa.Column("payload", JSONB()),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "submission_id"],
            ["evaluation_jobs.job_id", "evaluation_jobs.submission_id"],
            name="fk_raw_job_submission",
        ),
        sa.UniqueConstraint("execution_id", name="uq_raw_execution"),
        sa.CheckConstraint(
            "status IN ('READY','FAILED','INVALID','UNKNOWN')", name="ck_raw_evaluation_status"
        ),
        sa.CheckConstraint(
            "(status = 'READY' AND payload IS NOT NULL AND error_code IS NULL) OR (status <> 'READY' AND payload IS NULL AND error_code IS NOT NULL)",
            name="ck_raw_evaluation_payload",
        ),
        sa.CheckConstraint("jsonb_typeof(input_refs) = 'object'", name="ck_raw_evaluation_refs"),
    )
    op.create_index(
        "uq_raw_evaluation_ready_job",
        "raw_evaluations",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'READY'"),
    )
    op.execute("""CREATE FUNCTION guard_evaluation_job() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'evaluation jobs must be preserved'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM evaluation_plans p JOIN submissions s ON s.submission_id=NEW.submission_id
          JOIN session_tasks t ON t.session_task_id=s.session_task_id
          WHERE p.paper_entry_id=NEW.paper_entry_id AND t.paper_entry_id=p.paper_entry_id
          AND NEW.requirement_index >= 0 AND p.requirements -> NEW.requirement_index = NEW.requirement) THEN
          RAISE EXCEPTION 'evaluation job must match frozen plan'; END IF;
      ELSE
        IF (NEW.job_id,NEW.intent_id,NEW.submission_id,NEW.paper_entry_id,NEW.requirement_index,NEW.requirement,NEW.created_at)
          IS DISTINCT FROM (OLD.job_id,OLD.intent_id,OLD.submission_id,OLD.paper_entry_id,OLD.requirement_index,OLD.requirement,OLD.created_at)
          OR (OLD.input_fingerprint IS NOT NULL AND (NEW.input_refs,NEW.input_fingerprint) IS DISTINCT FROM (OLD.input_refs,OLD.input_fingerprint))
          OR OLD.status='READY' THEN RAISE EXCEPTION 'evaluation job identity or success is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_evaluation_job BEFORE INSERT OR UPDATE OR DELETE ON evaluation_jobs FOR EACH ROW EXECUTE FUNCTION guard_evaluation_job()"
    )
    op.execute("""CREATE FUNCTION reject_raw_evaluation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'raw evaluation is immutable'; END $$""")
    op.execute(
        "CREATE TRIGGER reject_raw_evaluation_mutation BEFORE UPDATE OR DELETE ON raw_evaluations FOR EACH ROW EXECUTE FUNCTION reject_raw_evaluation_mutation()"
    )
    op.execute("""CREATE FUNCTION check_evaluation_ready() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE jid uuid;
    BEGIN
      jid := NEW.job_id;
      IF EXISTS (SELECT 1 FROM evaluation_jobs WHERE job_id=jid AND status='READY') <>
         EXISTS (SELECT 1 FROM raw_evaluations WHERE job_id=jid AND status='READY') THEN
        RAISE EXCEPTION 'ready job and raw evaluation must coexist'; END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_job_raw AFTER INSERT OR UPDATE ON evaluation_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_evaluation_ready()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_raw_job AFTER INSERT ON raw_evaluations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_evaluation_ready()"
    )


def downgrade():
    op.execute("LOCK TABLE evaluation_jobs, raw_evaluations IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM evaluation_jobs) OR EXISTS (SELECT 1 FROM raw_evaluations) THEN
      RAISE EXCEPTION 'cannot downgrade: evaluation facts must be preserved'; END IF; END $$""")
    op.drop_table("raw_evaluations")
    op.drop_table("evaluation_jobs")
    op.execute("DROP FUNCTION check_evaluation_ready()")
    op.execute("DROP FUNCTION reject_raw_evaluation_mutation()")
    op.execute("DROP FUNCTION guard_evaluation_job()")
    op.drop_constraint("uq_intent_identity", "evaluation_intents", type_="unique")
