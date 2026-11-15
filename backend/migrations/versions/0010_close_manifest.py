"""Freeze the unique settlement input and a durable settlement intent."""

import sqlalchemy as sa
from alembic import op

revision = "0010_close_manifest"
down_revision = "0009_submission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("LOCK TABLE assessment_sessions IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM assessment_sessions WHERE status <> 'IN_PROGRESS') THEN
        RAISE EXCEPTION 'cannot migrate closed sessions without manifest reconciliation'; END IF; END $$""")
    op.create_unique_constraint(
        "uq_session_task_session", "session_tasks", ["session_task_id", "session_id"]
    )
    op.create_unique_constraint(
        "uq_submission_task", "submissions", ["submission_id", "session_task_id"]
    )
    op.create_table(
        "close_manifests",
        sa.Column("manifest_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id", sa.Uuid(), sa.ForeignKey("assessment_sessions.session_id"), nullable=False
        ),
        sa.Column("close_request_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("session_id", name="uq_manifest_session"),
        sa.UniqueConstraint("manifest_id", "session_id", name="uq_manifest_session_pair"),
    )
    op.create_table(
        "close_manifest_entries",
        sa.Column("manifest_id", sa.Uuid(), primary_key=True),
        sa.Column("session_task_id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["manifest_id", "session_id"],
            ["close_manifests.manifest_id", "close_manifests.session_id"],
            name="fk_manifest_entry_manifest_session",
        ),
        sa.ForeignKeyConstraint(
            ["session_task_id", "session_id"],
            ["session_tasks.session_task_id", "session_tasks.session_id"],
            name="fk_manifest_entry_task_session",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "session_task_id"],
            ["submissions.submission_id", "submissions.session_task_id"],
            name="fk_manifest_entry_submission_task",
        ),
        sa.UniqueConstraint("manifest_id", "submission_id", name="uq_manifest_entry_submission"),
    )
    op.create_table(
        "settlement_intents",
        sa.Column(
            "manifest_id", sa.Uuid(), sa.ForeignKey("close_manifests.manifest_id"), primary_key=True
        ),
    )
    op.execute("""CREATE FUNCTION guard_manifest_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'close manifest is immutable'; END IF;
      IF NOT EXISTS (SELECT 1 FROM assessment_sessions WHERE session_id=NEW.session_id AND status='IN_PROGRESS') THEN
        RAISE EXCEPTION 'session input is closed'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_manifest BEFORE INSERT OR UPDATE OR DELETE ON close_manifests FOR EACH ROW EXECUTE FUNCTION guard_manifest_fact()"
    )
    op.execute(
        "CREATE TRIGGER guard_manifest_entry BEFORE INSERT OR UPDATE OR DELETE ON close_manifest_entries FOR EACH ROW EXECUTE FUNCTION guard_manifest_fact()"
    )
    op.execute("""CREATE FUNCTION guard_closed_session() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (OLD.status='INPUT_CLOSED' AND NEW.status='IN_PROGRESS') OR
         (OLD.status='COMPLETED' AND NEW.status <> 'COMPLETED') THEN
        RAISE EXCEPTION 'closed session cannot be reopened'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_closed_session BEFORE UPDATE ON assessment_sessions FOR EACH ROW EXECUTE FUNCTION guard_closed_session()"
    )
    op.execute("""CREATE FUNCTION guard_closed_session_task() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE sid uuid;
    BEGIN
      IF TG_OP = 'INSERT' THEN sid := NEW.session_id; ELSE sid := OLD.session_id; END IF;
      IF EXISTS (SELECT 1 FROM assessment_sessions WHERE session_id=sid AND status <> 'IN_PROGRESS') OR
         (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM assessment_sessions WHERE session_id=NEW.session_id AND status <> 'IN_PROGRESS')) THEN
        RAISE EXCEPTION 'session input is closed'; END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_closed_session_task BEFORE INSERT OR UPDATE OR DELETE ON session_tasks FOR EACH ROW EXECUTE FUNCTION guard_closed_session_task()"
    )
    op.execute("""CREATE FUNCTION guard_closed_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE tid uuid;
    BEGIN
      IF TG_OP = 'INSERT' THEN tid := NEW.session_task_id; ELSE tid := OLD.session_task_id; END IF;
      IF EXISTS (SELECT 1 FROM session_tasks t JOIN assessment_sessions s ON s.session_id=t.session_id
                 WHERE t.session_task_id=tid AND s.status <> 'IN_PROGRESS') OR
         (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM session_tasks t JOIN assessment_sessions s ON s.session_id=t.session_id
                 WHERE t.session_task_id=NEW.session_task_id AND s.status <> 'IN_PROGRESS')) THEN
        RAISE EXCEPTION 'session input is closed'; END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_closed_attempt BEFORE INSERT OR UPDATE OR DELETE ON attempts FOR EACH ROW EXECUTE FUNCTION guard_closed_attempt()"
    )


def downgrade():
    op.execute(
        "LOCK TABLE assessment_sessions, close_manifests, close_manifest_entries, settlement_intents IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM close_manifests) OR EXISTS (SELECT 1 FROM close_manifest_entries) OR EXISTS (SELECT 1 FROM settlement_intents) OR EXISTS (SELECT 1 FROM assessment_sessions WHERE status <> 'IN_PROGRESS') THEN
      RAISE EXCEPTION 'cannot downgrade: close manifest facts must be preserved'; END IF; END $$""")
    op.execute("DROP TRIGGER guard_closed_attempt ON attempts")
    op.execute("DROP TRIGGER guard_closed_session_task ON session_tasks")
    op.execute("DROP TRIGGER guard_closed_session ON assessment_sessions")
    op.drop_table("settlement_intents")
    op.drop_table("close_manifest_entries")
    op.drop_table("close_manifests")
    op.execute("DROP FUNCTION guard_closed_session_task()")
    op.execute("DROP FUNCTION guard_closed_attempt()")
    op.execute("DROP FUNCTION guard_closed_session()")
    op.execute("DROP FUNCTION guard_manifest_fact()")
    op.drop_constraint("uq_submission_task", "submissions", type_="unique")
    op.drop_constraint("uq_session_task_session", "session_tasks", type_="unique")
