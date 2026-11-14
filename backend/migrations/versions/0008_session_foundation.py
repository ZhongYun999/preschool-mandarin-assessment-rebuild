"""Create session and candidate attempt identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_session_foundation"
down_revision = "0007_tts_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_paper_entry_paper", "paper_entries", ["paper_entry_id", "paper_id"]
    )
    op.create_table(
        "assessment_sessions",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("create_request_id", sa.Text(), nullable=False),
        sa.Column("review_policy", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="IN_PROGRESS"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("scope", "create_request_id", name="uq_session_create_request"),
        sa.UniqueConstraint("session_id", "paper_id", name="uq_session_paper"),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'INPUT_CLOSED', 'COMPLETED')", name="ck_session_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(review_policy) = 'object' AND review_policy <> '{}'::jsonb",
            name="ck_session_review_policy",
        ),
    )
    op.create_table(
        "session_tasks",
        sa.Column("session_task_id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("paper_entry_id", sa.Uuid(), nullable=False),
        sa.Column("current_attempt_id", sa.Uuid()),
        sa.UniqueConstraint("session_id", "paper_entry_id", name="uq_session_task_entry"),
        sa.ForeignKeyConstraint(
            ["session_id", "paper_id"],
            ["assessment_sessions.session_id", "assessment_sessions.paper_id"],
            name="fk_session_task_session_paper",
        ),
        sa.ForeignKeyConstraint(
            ["paper_entry_id", "paper_id"],
            ["paper_entries.paper_entry_id", "paper_entries.paper_id"],
            name="fk_session_task_entry_paper",
        ),
    )
    op.create_table(
        "attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_task_id",
            sa.Uuid(),
            sa.ForeignKey("session_tasks.session_task_id"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"),
        sa.Column("redo_request_id", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("attempt_id", "session_task_id", name="uq_attempt_task"),
        sa.UniqueConstraint("session_task_id", "redo_request_id", name="uq_attempt_redo_request"),
        sa.CheckConstraint("status IN ('OPEN', 'SEALED', 'ABANDONED')", name="ck_attempt_status"),
    )
    op.create_foreign_key(
        "fk_session_task_current_attempt",
        "session_tasks",
        "attempts",
        ["current_attempt_id", "session_task_id"],
        ["attempt_id", "session_task_id"],
    )
    op.execute("""CREATE FUNCTION guard_session_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.session_id, NEW.paper_id, NEW.scope, NEW.create_request_id, NEW.review_policy, NEW.created_at)
         IS DISTINCT FROM
         (OLD.session_id, OLD.paper_id, OLD.scope, OLD.create_request_id, OLD.review_policy, OLD.created_at) THEN
        RAISE EXCEPTION 'session identity and review policy are immutable';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_session_identity BEFORE UPDATE ON assessment_sessions "
        "FOR EACH ROW EXECUTE FUNCTION guard_session_identity()"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE assessment_sessions, session_tasks, attempts IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM assessment_sessions)
      OR EXISTS (SELECT 1 FROM session_tasks) OR EXISTS (SELECT 1 FROM attempts) THEN
      RAISE EXCEPTION 'cannot downgrade: session and attempt facts must be preserved';
      END IF; END $$""")
    op.drop_constraint("fk_session_task_current_attempt", "session_tasks", type_="foreignkey")
    op.drop_table("attempts")
    op.drop_table("session_tasks")
    op.drop_table("assessment_sessions")
    op.execute("DROP FUNCTION guard_session_identity()")
    op.drop_constraint("uq_paper_entry_paper", "paper_entries", type_="unique")
