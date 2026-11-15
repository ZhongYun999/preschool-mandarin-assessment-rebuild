"""Preserve SessionTask identity and reject missing historical paper entries."""

from alembic import op

revision = "0011_session_task_identity"
down_revision = "0010_close_manifest"
branch_labels = None
depends_on = None


def upgrade():
    # Serialize preflight with creation and direct writes to these relationships.
    op.execute(
        "LOCK TABLE assessment_sessions, paper_entries, session_tasks IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM assessment_sessions s
        JOIN paper_entries e ON e.paper_id = s.paper_id
        LEFT JOIN session_tasks t ON t.session_id = s.session_id
                                 AND t.paper_entry_id = e.paper_entry_id
        WHERE t.session_task_id IS NULL
      ) THEN
        RAISE EXCEPTION 'cannot migrate: session tasks require reconciliation';
      END IF;
    END $$""")
    op.execute("""CREATE FUNCTION guard_session_task_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'session task identity must be preserved' USING ERRCODE = '23514';
      END IF;
      IF (NEW.session_task_id, NEW.session_id, NEW.paper_id, NEW.paper_entry_id)
         IS DISTINCT FROM
         (OLD.session_task_id, OLD.session_id, OLD.paper_id, OLD.paper_entry_id) THEN
        RAISE EXCEPTION 'session task identity is immutable' USING ERRCODE = '23514';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_session_task_identity BEFORE UPDATE OR DELETE ON session_tasks FOR EACH ROW EXECUTE FUNCTION guard_session_task_identity()"
    )


def downgrade():
    op.execute("DROP TRIGGER guard_session_task_identity ON session_tasks")
    op.execute("DROP FUNCTION guard_session_task_identity()")
