"""Add the current Speaking submission-duration policy."""

import sqlalchemy as sa
from alembic import op

revision = "0023_speaking_policy"
down_revision = "0022_guidance_audio"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "speaking_runtime_policy",
        sa.Column("policy_id", sa.Integer(), primary_key=True),
        sa.Column("minimum_duration_seconds", sa.Float(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("policy_id = 1", name="ck_speaking_policy_singleton"),
        sa.CheckConstraint(
            "minimum_duration_seconds > 0 AND minimum_duration_seconds < 'Infinity'::float8",
            name="ck_speaking_policy_duration",
        ),
        sa.CheckConstraint("revision > 0", name="ck_speaking_policy_revision"),
    )


def downgrade():
    op.execute("LOCK TABLE speaking_runtime_policy IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_runtime_policy) THEN RAISE EXCEPTION 'speaking policy must be preserved'; END IF; END $$"
    )
    op.drop_table("speaking_runtime_policy")
