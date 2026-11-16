"""Preserve revocation facts across schema changes.

Revision ID: 0017_credential_revocation
Revises: 0016_session_credentials
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_credential_revocation"
down_revision: str | None = "0016_session_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_credentials", sa.Column("revoked_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.execute("LOCK TABLE session_credentials IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM session_credentials WHERE revoked_at IS NOT NULL) THEN
        RAISE EXCEPTION 'cannot downgrade: credential revocations must be preserved';
    END IF; END $$""")
    op.drop_column("session_credentials", "revoked_at")
