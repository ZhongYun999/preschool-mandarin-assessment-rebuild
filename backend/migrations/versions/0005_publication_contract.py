"""Freeze per-entry evaluation plans before publication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_publication_contract"
down_revision = "0004_paper_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM papers WHERE status <> 'DRAFT') THEN
        RAISE EXCEPTION 'existing published papers lack evaluation contracts; migration requires explicit reconciliation';
      END IF;
    END $$""")
    op.create_table(
        "evaluation_plans",
        sa.Column(
            "paper_entry_id",
            sa.Uuid(),
            sa.ForeignKey("paper_entries.paper_entry_id"),
            primary_key=True,
        ),
        sa.Column(
            "task_revision_id",
            sa.Uuid(),
            sa.ForeignKey("task_revisions.task_revision_id"),
            nullable=False,
        ),
        sa.Column("plugin_id", sa.Text(), nullable=False),
        sa.Column("plugin_version", sa.Text(), nullable=False),
        sa.Column("requirements", JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(requirements) = 'array'", name="ck_evaluation_requirements_array"
        ),
    )
    op.execute("""CREATE FUNCTION guard_evaluation_plan() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE entry_id uuid; revision_id uuid; p papers%ROWTYPE;
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW.paper_entry_id <> OLD.paper_entry_id THEN
        RAISE EXCEPTION 'cannot move an evaluation plan';
      END IF;
      IF TG_OP = 'DELETE' THEN entry_id := OLD.paper_entry_id; ELSE entry_id := NEW.paper_entry_id; END IF;
      SELECT paper_id, task_revision_id INTO p.paper_id, revision_id FROM paper_entries WHERE paper_entry_id=entry_id;
      SELECT * INTO p FROM papers WHERE paper_id=p.paper_id FOR UPDATE;
      IF p.paper_id IS NULL OR p.status <> 'DRAFT' THEN RAISE EXCEPTION 'evaluation plan is frozen'; END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      IF NEW.task_revision_id IS DISTINCT FROM revision_id OR NEW.plugin_id IS DISTINCT FROM p.scoring_plugin_id
        OR NEW.plugin_version IS DISTINCT FROM p.scoring_plugin_version THEN
        RAISE EXCEPTION 'evaluation plan must match entry and plugin';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER evaluation_plan_guard BEFORE INSERT OR UPDATE OR DELETE ON evaluation_plans FOR EACH ROW EXECUTE FUNCTION guard_evaluation_plan()"
    )
    op.execute("""CREATE FUNCTION guard_publication_contract() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND (NEW.scoring_plugin_id, NEW.scoring_plugin_version) IS DISTINCT FROM (OLD.scoring_plugin_id, OLD.scoring_plugin_version)
        AND EXISTS (SELECT 1 FROM paper_entries e JOIN evaluation_plans p USING (paper_entry_id) WHERE e.paper_id=NEW.paper_id) THEN
        RAISE EXCEPTION 'remove draft evaluation plans before changing plugin';
      END IF;
      IF NEW.status IN ('PUBLISHED', 'ARCHIVED') THEN
        IF NEW.scoring_plugin_id IS NULL OR EXISTS (
          SELECT 1 FROM paper_entries e LEFT JOIN evaluation_plans p USING (paper_entry_id)
          WHERE e.paper_id=NEW.paper_id AND (p.paper_entry_id IS NULL OR p.task_revision_id IS DISTINCT FROM e.task_revision_id
            OR p.plugin_id IS DISTINCT FROM NEW.scoring_plugin_id OR p.plugin_version IS DISTINCT FROM NEW.scoring_plugin_version)
        ) THEN RAISE EXCEPTION 'published paper requires complete frozen contracts'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER publication_contract_guard BEFORE INSERT OR UPDATE ON papers FOR EACH ROW EXECUTE FUNCTION guard_publication_contract()"
    )
    op.execute("""CREATE FUNCTION guard_entry_plan_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.task_revision_id IS DISTINCT FROM OLD.task_revision_id AND EXISTS (SELECT 1 FROM evaluation_plans WHERE paper_entry_id=OLD.paper_entry_id) THEN
        RAISE EXCEPTION 'remove draft evaluation plan before replacing entry';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER entry_plan_revision_guard BEFORE UPDATE ON paper_entries FOR EACH ROW EXECUTE FUNCTION guard_entry_plan_revision()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER entry_plan_revision_guard ON paper_entries")
    op.execute("DROP FUNCTION guard_entry_plan_revision()")
    op.execute("DROP TRIGGER publication_contract_guard ON papers")
    op.execute("DROP FUNCTION guard_publication_contract()")
    op.execute("DROP TRIGGER evaluation_plan_guard ON evaluation_plans")
    op.execute("DROP FUNCTION guard_evaluation_plan()")
    op.drop_table("evaluation_plans")
