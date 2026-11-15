"""Playback and immutable accepted submissions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_submission"
down_revision = "0008_session_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("LOCK TABLE attempts IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM attempts WHERE status='SEALED') THEN
        RAISE EXCEPTION 'cannot migrate SEALED attempts without submission reconciliation';
        END IF; END $$""")
    op.create_table(
        "playbacks",
        sa.Column("playback_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column(
            "audio_id", sa.Uuid(), sa.ForeignKey("fixed_audio_requests.audio_id"), nullable=False
        ),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("retry_of_id", sa.Uuid(), sa.ForeignKey("playbacks.playback_id")),
        sa.UniqueConstraint("attempt_id", "request_id", name="uq_playback_request"),
        sa.CheckConstraint(
            "kind IN ('GUIDE','INITIAL_PROMPT','USER_REPLAY','TECHNICAL_RETRY')",
            name="ck_playback_kind",
        ),
        sa.CheckConstraint("status IN ('STARTED','COMPLETED','FAILED')", name="ck_playback_status"),
    )
    op.create_table(
        "submissions",
        sa.Column("submission_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column(
            "session_task_id",
            sa.Uuid(),
            sa.ForeignKey("session_tasks.session_task_id"),
            nullable=False,
        ),
        sa.Column("submit_request_id", sa.Text(), nullable=False),
        sa.Column("inputs", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "session_task_id"],
            ["attempts.attempt_id", "attempts.session_task_id"],
            name="fk_submission_attempt_task",
        ),
        sa.UniqueConstraint("attempt_id", name="uq_submission_attempt"),
        sa.UniqueConstraint("session_task_id", "submit_request_id", name="uq_submission_request"),
        sa.CheckConstraint("jsonb_typeof(inputs) = 'object'", name="ck_submission_inputs"),
    )
    op.create_table(
        "evaluation_intents",
        sa.Column("intent_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "submission_id", sa.Uuid(), sa.ForeignKey("submissions.submission_id"), nullable=False
        ),
        sa.Column(
            "paper_entry_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_plans.paper_entry_id"),
            nullable=False,
        ),
        sa.Column("requirement_index", sa.Integer(), nullable=False),
        sa.UniqueConstraint("submission_id", "requirement_index", name="uq_intent_requirement"),
    )
    op.execute("""CREATE FUNCTION check_submission_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE aid uuid;
    BEGIN
      IF TG_TABLE_NAME = 'attempts' THEN aid := NEW.attempt_id;
      ELSE aid := NEW.attempt_id; END IF;
      IF EXISTS (SELECT 1 FROM attempts WHERE attempt_id=aid AND status='SEALED') <>
         EXISTS (SELECT 1 FROM submissions WHERE attempt_id=aid) THEN
        RAISE EXCEPTION 'sealed attempt and submission must coexist';
      END IF;
      RETURN NULL;
    END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_attempt_submission AFTER INSERT OR UPDATE ON attempts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_submission_sealed()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER check_submission_attempt AFTER INSERT ON submissions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_submission_sealed()"
    )
    op.execute("""CREATE FUNCTION reject_submission_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'submission is immutable'; END $$""")
    op.execute(
        "CREATE TRIGGER reject_submission_mutation BEFORE UPDATE OR DELETE ON submissions FOR EACH ROW EXECUTE FUNCTION reject_submission_mutation()"
    )
    op.execute("""CREATE FUNCTION guard_playback_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'playback facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM attempts WHERE attempt_id=NEW.attempt_id AND status='OPEN') THEN
        RAISE EXCEPTION 'playback attempt is not open';
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'STARTED' THEN RAISE EXCEPTION 'playback must start first'; END IF;
      ELSE
        IF OLD.status <> 'STARTED' OR NEW.status NOT IN ('COMPLETED', 'FAILED') OR
          (NEW.playback_id, NEW.attempt_id, NEW.audio_id, NEW.request_id, NEW.kind, NEW.retry_of_id)
          IS DISTINCT FROM
          (OLD.playback_id, OLD.attempt_id, OLD.audio_id, OLD.request_id, OLD.kind, OLD.retry_of_id) THEN
          RAISE EXCEPTION 'playback fact is immutable';
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_playback_fact BEFORE INSERT OR UPDATE OR DELETE ON playbacks FOR EACH ROW EXECUTE FUNCTION guard_playback_fact()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE attempts, playbacks, submissions, evaluation_intents IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM playbacks) OR EXISTS (SELECT 1 FROM submissions) OR EXISTS (SELECT 1 FROM evaluation_intents) THEN RAISE EXCEPTION 'cannot downgrade: submission facts must be preserved'; END IF; END $$"""
    )
    op.execute("DROP TRIGGER check_attempt_submission ON attempts")
    op.drop_table("evaluation_intents")
    op.drop_table("submissions")
    op.drop_table("playbacks")
    op.execute("DROP FUNCTION check_submission_sealed()")
    op.execute("DROP FUNCTION reject_submission_mutation()")
    op.execute("DROP FUNCTION guard_playback_fact()")
