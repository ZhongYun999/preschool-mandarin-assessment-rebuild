"""Persist immutable recording evidence.

Revision ID: 0018_recordings
Revises: 0017_credential_revocation
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_recordings"
down_revision = "0017_credential_revocation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recordings",
        sa.Column("recording_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("storage_ref", sa.Text()),
        sa.Column("sha256", sa.Text()),
        sa.Column("media_type", sa.Text()),
        sa.UniqueConstraint("attempt_id", "request_id", name="uq_recording_request"),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SAVED', 'CANCELLED')", name="ck_recording_status"
        ),
        sa.CheckConstraint(
            "(status = 'SAVED' AND storage_ref IS NOT NULL AND sha256 IS NOT NULL AND media_type IS NOT NULL) OR (status <> 'SAVED' AND storage_ref IS NULL AND sha256 IS NULL AND media_type IS NULL)",
            name="ck_recording_metadata",
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="ck_recording_sha256"
        ),
    )
    op.create_index(
        "uq_recording_active_attempt",
        "recordings",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("status = 'STARTED'"),
    )
    op.execute("""CREATE FUNCTION guard_recording_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'recording facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM attempts WHERE attempt_id=NEW.attempt_id AND status='OPEN') THEN
        RAISE EXCEPTION 'recording attempt is not open'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'STARTED' THEN RAISE EXCEPTION 'recording must start first'; END IF;
      ELSE
        IF OLD.status <> 'STARTED' OR NEW.status NOT IN ('SAVED', 'CANCELLED') OR
           (NEW.recording_id, NEW.attempt_id, NEW.request_id) IS DISTINCT FROM
           (OLD.recording_id, OLD.attempt_id, OLD.request_id) THEN
          RAISE EXCEPTION 'recording fact is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_recording_fact BEFORE INSERT OR UPDATE OR DELETE ON recordings FOR EACH ROW EXECUTE FUNCTION guard_recording_fact()"
    )


def downgrade():
    op.execute("LOCK TABLE recordings IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM recordings) THEN
        RAISE EXCEPTION 'cannot downgrade: recording facts must be preserved'; END IF; END $$""")
    op.drop_table("recordings")
    op.execute("DROP FUNCTION guard_recording_fact()")
