"""Add teacher accounts and retain authenticated audit identities."""

import sqlalchemy as sa
from alembic import op

revision = "0027_auth_users_sessions"
down_revision = "0026_llm_credential_region"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_users",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('admin', 'teacher')", name="ck_auth_user_role"),
        sa.CheckConstraint("username ~ '^[a-z0-9._-]{1,64}$'", name="ck_auth_user_username"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_digest", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.add_column("review_decisions", sa.Column("reviewer_user_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_review_decisions_reviewer_user_id",
        "review_decisions",
        "auth_users",
        ["reviewer_user_id"],
        ["user_id"],
    )
    op.add_column("llm_provider_audit", sa.Column("actor_user_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_llm_provider_audit_actor_user_id",
        "llm_provider_audit",
        "auth_users",
        ["actor_user_id"],
        ["user_id"],
    )


def downgrade():
    op.execute("LOCK TABLE review_decisions, llm_provider_audit IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM review_decisions WHERE reviewer_user_id IS NOT NULL)
        OR EXISTS (SELECT 1 FROM llm_provider_audit WHERE actor_user_id IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot discard authenticated audit identity';
        END IF;
    END $$""")
    op.drop_constraint(
        "fk_llm_provider_audit_actor_user_id", "llm_provider_audit", type_="foreignkey"
    )
    op.drop_column("llm_provider_audit", "actor_user_id")
    op.drop_constraint(
        "fk_review_decisions_reviewer_user_id", "review_decisions", type_="foreignkey"
    )
    op.drop_column("review_decisions", "reviewer_user_id")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
