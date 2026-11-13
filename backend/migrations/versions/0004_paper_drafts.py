"""Add draft papers and protect published facts."""

import sqlalchemy as sa
from alembic import op

revision = "0004_paper_drafts"
down_revision = "0003_import_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("paper_id", sa.Uuid(), primary_key=True),
        sa.Column("bank_id", sa.Uuid(), sa.ForeignKey("task_banks.bank_id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
        sa.Column("scoring_plugin_id", sa.Text()),
        sa.Column("scoring_plugin_version", sa.Text()),
        sa.Column("edit_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')", name="ck_paper_status"),
        sa.CheckConstraint(
            "(scoring_plugin_id IS NULL) = (scoring_plugin_version IS NULL)",
            name="ck_paper_plugin_pair",
        ),
    )
    op.create_table(
        "paper_entries",
        sa.Column("paper_entry_id", sa.Uuid(), primary_key=True),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.paper_id"), nullable=False),
        sa.Column(
            "task_revision_id",
            sa.Uuid(),
            sa.ForeignKey("task_revisions.task_revision_id"),
            nullable=False,
        ),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "paper_id",
            "position",
            name="uq_paper_entry_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_paper_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN RAISE EXCEPTION 'published paper is immutable'; END IF;
        RETURN OLD;
      END IF;
      IF NEW.paper_id <> OLD.paper_id OR NEW.bank_id <> OLD.bank_id THEN
        RAISE EXCEPTION 'paper identity and bank are immutable';
      END IF;
      IF OLD.status <> 'DRAFT' THEN
        IF (NEW.paper_id, NEW.bank_id, NEW.name, NEW.description, NEW.scoring_plugin_id,
            NEW.scoring_plugin_version, NEW.edit_version, NEW.created_at)
           IS DISTINCT FROM
           (OLD.paper_id, OLD.bank_id, OLD.name, OLD.description, OLD.scoring_plugin_id,
            OLD.scoring_plugin_version, OLD.edit_version, OLD.created_at)
           OR NOT (NEW.status = OLD.status OR (OLD.status = 'PUBLISHED' AND NEW.status = 'ARCHIVED')) THEN
          RAISE EXCEPTION 'published paper is immutable';
        END IF;
      END IF;
      RETURN NEW;
    END $$
    """)
    op.execute(
        "CREATE TRIGGER paper_frozen BEFORE UPDATE OR DELETE ON papers FOR EACH ROW EXECUTE FUNCTION guard_paper_frozen()"
    )
    op.execute("""
    CREATE FUNCTION guard_paper_entry() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE parent_status text; parent_bank uuid; revision_bank uuid; parent_id uuid;
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW.paper_id <> OLD.paper_id THEN
        RAISE EXCEPTION 'cannot move a paper entry';
      END IF;
      IF TG_OP = 'DELETE' THEN parent_id := OLD.paper_id; ELSE parent_id := NEW.paper_id; END IF;
      SELECT status, bank_id INTO parent_status, parent_bank FROM papers WHERE paper_id = parent_id FOR UPDATE;
      IF parent_status IS NULL OR parent_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'paper entries are immutable after publication';
      END IF;
      IF TG_OP <> 'DELETE' THEN
        SELECT i.bank_id INTO revision_bank FROM task_revisions r JOIN task_identities i ON i.task_id = r.task_id
          WHERE r.task_revision_id = NEW.task_revision_id;
        IF revision_bank IS NULL OR revision_bank <> parent_bank THEN
          RAISE EXCEPTION 'paper entry must reference the paper bank';
        END IF;
        RETURN NEW;
      END IF;
      RETURN OLD;
    END $$
    """)
    op.execute(
        "CREATE TRIGGER paper_entry_guard BEFORE INSERT OR UPDATE OR DELETE ON paper_entries FOR EACH ROW EXECUTE FUNCTION guard_paper_entry()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER paper_entry_guard ON paper_entries")
    op.execute("DROP FUNCTION guard_paper_entry()")
    op.execute("DROP TRIGGER paper_frozen ON papers")
    op.execute("DROP FUNCTION guard_paper_frozen()")
    op.drop_table("paper_entries")
    op.drop_table("papers")
