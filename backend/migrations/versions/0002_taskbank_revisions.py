"""Create task-bank identities and immutable revisions."""
import sqlalchemy as sa
from alembic import op

revision = "0002_taskbank"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("task_banks", sa.Column("bank_id", sa.Uuid(), primary_key=True),
                    sa.Column("name", sa.Text(), nullable=False))
    op.create_table(
        "task_identities", sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("bank_id", sa.Uuid(), sa.ForeignKey("task_banks.bank_id"), nullable=False),
        sa.Column("task_code", sa.Text(), nullable=False), sa.Column("display_order", sa.BigInteger()),
        sa.UniqueConstraint("bank_id", "task_code", name="uq_bank_task_code"),
    )
    op.create_table(
        "task_revisions", sa.Column("task_revision_id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("task_identities.task_id"), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("grade_level", sa.Text(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False), sa.Column("guide_text", sa.Text()),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("target_rounds", sa.BigInteger()),
        sa.Column("material_description", sa.Text()), sa.Column("reference_answer", sa.Text()),
        sa.Column("scoring_instruction", sa.Text()), sa.Column("ai_followup_scope", sa.Text()),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "version", name="uq_task_revision_version"),
        sa.CheckConstraint("version > 0", name="ck_revision_version_positive"),
        sa.CheckConstraint("(target_rounds IS NULL OR target_rounds > 0) AND "
                           "(task_type <> 'dialogue' OR target_rounds IS NOT NULL)",
                           name="ck_dialogue_target_rounds"),
    )
    op.create_table(
        "assets", sa.Column("asset_id", sa.Uuid(), primary_key=True),
        sa.Column("sha256", sa.Text(), nullable=False), sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_asset_sha256"),
    )
    op.create_table(
        "task_revision_assets",
        sa.Column("task_revision_id", sa.Uuid(), sa.ForeignKey("task_revisions.task_revision_id"), primary_key=True),
        sa.Column("slot", sa.Text(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("assets.asset_id"), nullable=False),
        sa.CheckConstraint("slot IN ('option_A', 'option_B', 'option_C', 'question_image')",
                           name="ck_revision_asset_slot"),
    )
    op.create_index("ix_task_revision_assets_asset_id", "task_revision_assets", ["asset_id"])
    op.execute("""CREATE FUNCTION guard_task_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.task_id IS DISTINCT FROM OLD.task_id OR NEW.bank_id IS DISTINCT FROM OLD.bank_id
                OR NEW.task_code IS DISTINCT FROM OLD.task_code THEN
                RAISE EXCEPTION 'task identity cannot be moved or renamed';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER immutable_task_identity BEFORE UPDATE ON task_identities
        FOR EACH ROW EXECUTE FUNCTION guard_task_identity()""")
    op.execute("""CREATE FUNCTION reject_revision_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'historical revisions and image links are immutable'; END $$""")
    op.execute("""CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE ON task_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation()""")
    op.execute("""CREATE TRIGGER immutable_revision_asset BEFORE UPDATE OR DELETE ON task_revision_assets
        FOR EACH ROW EXECUTE FUNCTION reject_revision_mutation()""")
    op.execute("""CREATE FUNCTION guard_revision_asset_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE created_xid bigint;
        BEGIN
            SELECT xmin::text::bigint INTO created_xid FROM task_revisions
              WHERE task_revision_id = NEW.task_revision_id;
            IF created_xid IS DISTINCT FROM (pg_current_xact_id()::text::numeric % 4294967296)::bigint THEN
                RAISE EXCEPTION 'image links must be created with their revision';
            END IF;
            PERFORM 1 FROM assets WHERE asset_id = NEW.asset_id FOR UPDATE;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER revision_asset_insert BEFORE INSERT ON task_revision_assets
        FOR EACH ROW EXECUTE FUNCTION guard_revision_asset_insert()""")
    op.execute("""CREATE FUNCTION guard_referenced_asset() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM task_revision_assets WHERE asset_id = OLD.asset_id) THEN
                RAISE EXCEPTION 'referenced asset is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER immutable_referenced_asset BEFORE UPDATE OR DELETE ON assets
        FOR EACH ROW EXECUTE FUNCTION guard_referenced_asset()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER immutable_referenced_asset ON assets")
    op.execute("DROP TRIGGER revision_asset_insert ON task_revision_assets")
    op.execute("DROP TRIGGER immutable_revision_asset ON task_revision_assets")
    op.execute("DROP TRIGGER immutable_revision ON task_revisions")
    op.execute("DROP TRIGGER immutable_task_identity ON task_identities")
    op.execute("DROP FUNCTION guard_referenced_asset()")
    op.execute("DROP FUNCTION guard_revision_asset_insert()")
    op.execute("DROP FUNCTION reject_revision_mutation()")
    op.execute("DROP FUNCTION guard_task_identity()")
    op.drop_index("ix_task_revision_assets_asset_id", table_name="task_revision_assets")
    op.drop_table("task_revision_assets")
    op.drop_table("assets")
    op.drop_table("task_revisions")
    op.drop_table("task_identities")
    op.drop_table("task_banks")
