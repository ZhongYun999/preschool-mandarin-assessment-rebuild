"""Add dispatch boundary and fair discovery for evaluation jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0013_eval_dispatcher"
down_revision = "0012_evaluation_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("evaluation_intents", sa.Column("scheduled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "evaluation_jobs",
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("evaluation_jobs", sa.Column("dispatch_started_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_evaluation_job_status", "evaluation_jobs", type_="check")
    op.create_check_constraint(
        "ck_evaluation_job_status",
        "evaluation_jobs",
        "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN','SKIPPED')",
    )
    op.drop_constraint("ck_evaluation_job_claim_input", "evaluation_jobs", type_="check")
    op.create_check_constraint(
        "ck_evaluation_job_claim_input",
        "evaluation_jobs",
        "status IN ('WAITING','SKIPPED') OR input_fingerprint IS NOT NULL",
    )
    op.create_index(
        "ix_evaluation_jobs_waiting",
        "evaluation_jobs",
        ["last_checked_at", "job_id"],
        postgresql_where=sa.text("status = 'WAITING'"),
    )
    op.create_index(
        "ix_evaluation_jobs_expired",
        "evaluation_jobs",
        ["lease_until"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index(
        "ix_evaluation_intents_pending",
        "evaluation_intents",
        ["intent_id"],
        postgresql_where=sa.text("scheduled_at IS NULL"),
    )
    op.execute("""CREATE FUNCTION guard_evaluation_intent_schedule() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.scheduled_at IS NOT NULL AND NEW.scheduled_at IS DISTINCT FROM OLD.scheduled_at THEN
        RAISE EXCEPTION 'evaluation intent scheduling fact is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_evaluation_intent_schedule BEFORE UPDATE ON evaluation_intents FOR EACH ROW EXECUTE FUNCTION guard_evaluation_intent_schedule()"
    )
    op.execute("""CREATE FUNCTION check_intent_job_materialized() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE iid uuid;
    BEGIN
      iid := NEW.intent_id;
      IF EXISTS (SELECT 1 FROM evaluation_intents WHERE intent_id=iid AND scheduled_at IS NOT NULL) <>
         EXISTS (SELECT 1 FROM evaluation_jobs WHERE intent_id=iid) THEN
        RAISE EXCEPTION 'evaluation intent and job must be scheduled atomically'; END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_intent_job_materialized AFTER INSERT OR UPDATE ON evaluation_intents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_intent_job_materialized()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_job_intent_materialized AFTER INSERT ON evaluation_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_intent_job_materialized()"
    )
    # Backfill the queue marker without treating existing jobs as new work.
    op.execute(
        "UPDATE evaluation_intents i SET scheduled_at=j.created_at FROM evaluation_jobs j WHERE j.intent_id=i.intent_id"
    )
    # EVAL-01A could already have invoked a provider for any running lease.
    # Perform this after table DDL: the UPDATE queues a deferred constraint trigger.
    op.execute(
        "UPDATE evaluation_jobs SET dispatch_started_at=clock_timestamp() WHERE status='RUNNING'"
    )


def downgrade():
    op.execute("LOCK TABLE evaluation_intents, evaluation_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM evaluation_jobs WHERE status IN ('SKIPPED','RUNNING') OR dispatch_started_at IS NOT NULL) THEN
      RAISE EXCEPTION 'cannot downgrade: dispatch facts must be preserved'; END IF; END $$""")
    op.execute("DROP TRIGGER check_job_intent_materialized ON evaluation_jobs")
    op.execute("DROP TRIGGER check_intent_job_materialized ON evaluation_intents")
    op.execute("DROP TRIGGER guard_evaluation_intent_schedule ON evaluation_intents")
    op.execute("DROP FUNCTION check_intent_job_materialized()")
    op.execute("DROP FUNCTION guard_evaluation_intent_schedule()")
    op.drop_index("ix_evaluation_intents_pending", table_name="evaluation_intents")
    op.drop_index("ix_evaluation_jobs_expired", table_name="evaluation_jobs")
    op.drop_index("ix_evaluation_jobs_waiting", table_name="evaluation_jobs")
    op.drop_constraint("ck_evaluation_job_claim_input", "evaluation_jobs", type_="check")
    op.create_check_constraint(
        "ck_evaluation_job_claim_input",
        "evaluation_jobs",
        "status = 'WAITING' OR input_fingerprint IS NOT NULL",
    )
    op.drop_constraint("ck_evaluation_job_status", "evaluation_jobs", type_="check")
    op.create_check_constraint(
        "ck_evaluation_job_status",
        "evaluation_jobs",
        "status IN ('WAITING','RUNNING','READY','FAILED','INVALID','UNKNOWN')",
    )
    op.drop_column("evaluation_jobs", "dispatch_started_at")
    op.drop_column("evaluation_jobs", "last_checked_at")
    op.drop_column("evaluation_intents", "scheduled_at")
