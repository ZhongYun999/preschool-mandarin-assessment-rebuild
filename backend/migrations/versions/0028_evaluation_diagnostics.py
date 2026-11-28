"""Keep immutable diagnostics for individual unsuccessful evaluations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_evaluation_diagnostics"
down_revision = "0027_auth_users_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evaluation_diagnostic_evidence",
        sa.Column(
            "evaluation_id",
            sa.Uuid(),
            sa.ForeignKey("raw_evaluations.evaluation_id"),
            primary_key=True,
        ),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'", name="ck_evaluation_diagnostic_object"
        ),
    )
    op.execute("""CREATE FUNCTION guard_evaluation_diagnostic() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'evaluation diagnostics are immutable'; END IF;
      IF NOT EXISTS (SELECT 1 FROM raw_evaluations WHERE evaluation_id=NEW.evaluation_id AND status <> 'READY') THEN
        RAISE EXCEPTION 'diagnostic requires unsuccessful evaluation';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER guard_evaluation_diagnostic BEFORE INSERT OR UPDATE OR DELETE
      ON evaluation_diagnostic_evidence FOR EACH ROW EXECUTE FUNCTION guard_evaluation_diagnostic()""")


def downgrade():
    op.execute("LOCK TABLE evaluation_diagnostic_evidence IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM evaluation_diagnostic_evidence) THEN
      RAISE EXCEPTION 'cannot discard evaluation diagnostic history'; END IF; END $$""")
    op.drop_table("evaluation_diagnostic_evidence")
    op.execute("DROP FUNCTION guard_evaluation_diagnostic()")
