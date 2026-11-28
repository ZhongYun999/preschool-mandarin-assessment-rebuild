"""Persist speaking checkpoint A facts.

Revision ID: 0019_speaking_segments
Revises: 0018_recordings
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_speaking_segments"
down_revision = "0018_recordings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "speaking_segments",
        sa.Column("segment_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column(
            "recording_id", sa.Uuid(), sa.ForeignKey("recordings.recording_id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.UniqueConstraint("attempt_id", "position", name="uq_speaking_segment_position"),
        sa.UniqueConstraint("recording_id", name="uq_speaking_segment_recording"),
        sa.CheckConstraint("position > 0", name="ck_speaking_segment_position"),
        sa.CheckConstraint("duration_seconds > 0", name="ck_speaking_segment_duration"),
    )
    op.execute("""CREATE FUNCTION guard_speaking_segment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'speaking segment facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM recordings WHERE recording_id=NEW.recording_id
                     AND attempt_id=NEW.attempt_id AND status='SAVED') THEN
        RAISE EXCEPTION 'segment recording is not saved for attempt';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_speaking_segment BEFORE INSERT OR UPDATE OR DELETE ON speaking_segments FOR EACH ROW EXECUTE FUNCTION guard_speaking_segment()"
    )


def downgrade():
    op.execute("LOCK TABLE speaking_segments IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_segments) THEN RAISE EXCEPTION 'speaking facts must be preserved'; END IF; END $$"
    )
    op.drop_table("speaking_segments")
    op.execute("DROP FUNCTION guard_speaking_segment()")
