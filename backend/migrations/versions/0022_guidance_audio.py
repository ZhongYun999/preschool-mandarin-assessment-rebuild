"""Speaking dynamic guidance audio and playback resource identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022_guidance_audio"
down_revision = "0021_speaking_help"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_help_requests WHERE status='DELIVERED')
       THEN RAISE EXCEPTION 'existing delivered guidance requires explicit reconciliation'; END IF; END $$""")
    op.create_table(
        "speaking_guidance_audio",
        sa.Column("audio_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "help_request_id",
            sa.Uuid(),
            sa.ForeignKey("speaking_help_requests.help_request_id"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("config", JSONB),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.Uuid()),
        sa.Column("storage_ref", sa.Text()),
        sa.Column("content_sha256", sa.Text()),
        sa.Column("media_type", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("history", JSONB, nullable=False),
        sa.Column("retry_request_id", sa.Text()),
        sa.Column("retry_expected_attempts", sa.Integer()),
        sa.UniqueConstraint("help_request_id", name="uq_guidance_audio_help"),
        sa.CheckConstraint(
            "status IN ('WAITING','SENDING','SAVED','READY','FAILED','UNKNOWN')",
            name="ck_guidance_audio_status",
        ),
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_guidance_audio_text_sha"),
        sa.CheckConstraint("jsonb_typeof(history) = 'array'", name="ck_guidance_audio_history"),
        sa.CheckConstraint(
            "(status IN ('SAVED','READY') AND storage_ref IS NOT NULL AND content_sha256 IS NOT NULL AND media_type IS NOT NULL AND content_sha256 ~ '^[0-9a-f]{64}$' AND media_type = 'audio/wav') OR (status NOT IN ('SAVED','READY') AND storage_ref IS NULL AND content_sha256 IS NULL AND media_type IS NULL)",
            name="ck_guidance_audio_result",
        ),
    )
    op.execute("""INSERT INTO speaking_guidance_audio
       (audio_id,help_request_id,attempt_id,text_sha256,status,attempts,history)
       SELECT gen_random_uuid(),help_request_id,attempt_id,
              encode(sha256(convert_to(guidance_text,'UTF8')),'hex'), 'WAITING',0,'[]'::jsonb
       FROM speaking_help_requests WHERE status='READY'""")
    op.alter_column("playbacks", "audio_id", nullable=True)
    op.add_column(
        "playbacks",
        sa.Column(
            "guidance_audio_id", sa.Uuid(), sa.ForeignKey("speaking_guidance_audio.audio_id")
        ),
    )
    op.drop_constraint("ck_playback_kind", "playbacks", type_="check")
    op.create_check_constraint(
        "ck_playback_kind",
        "playbacks",
        "kind IN ('GUIDE','INITIAL_PROMPT','USER_REPLAY','TECHNICAL_RETRY','GUIDANCE')",
    )
    op.create_check_constraint(
        "ck_playback_resource", "playbacks", "(audio_id IS NULL) <> (guidance_audio_id IS NULL)"
    )
    op.execute("""CREATE OR REPLACE FUNCTION guard_playback_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'playback facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM attempts WHERE attempt_id=NEW.attempt_id AND status='OPEN') THEN
        RAISE EXCEPTION 'playback attempt is not open'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'STARTED' THEN RAISE EXCEPTION 'playback must start first'; END IF;
      ELSE
        IF OLD.status <> 'STARTED' OR NEW.status NOT IN ('COMPLETED','FAILED') OR
          (NEW.playback_id,NEW.attempt_id,NEW.audio_id,NEW.guidance_audio_id,NEW.request_id,NEW.kind,NEW.retry_of_id)
          IS DISTINCT FROM (OLD.playback_id,OLD.attempt_id,OLD.audio_id,OLD.guidance_audio_id,OLD.request_id,OLD.kind,OLD.retry_of_id) THEN
          RAISE EXCEPTION 'playback fact is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION guard_guidance_audio() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'guidance audio facts must be preserved'; END IF;
      IF (OLD.audio_id,OLD.help_request_id,OLD.attempt_id,OLD.text_sha256) IS DISTINCT FROM
         (NEW.audio_id,NEW.help_request_id,NEW.attempt_id,NEW.text_sha256)
         OR (OLD.config IS NOT NULL AND OLD.config IS DISTINCT FROM NEW.config)
         OR (OLD.status='READY' AND OLD IS DISTINCT FROM NEW)
         OR (OLD.status='SAVED' AND (NEW.status <> 'READY' OR
             (to_jsonb(OLD) - 'status') IS DISTINCT FROM (to_jsonb(NEW) - 'status')))
         OR (SELECT COALESCE(jsonb_agg(value ORDER BY ord), '[]'::jsonb)
             FROM jsonb_array_elements(NEW.history) WITH ORDINALITY AS e(value,ord)
             WHERE ord <= jsonb_array_length(OLD.history)) IS DISTINCT FROM OLD.history
         OR NEW.attempts < OLD.attempts THEN RAISE EXCEPTION 'guidance audio facts are immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_guidance_audio BEFORE UPDATE OR DELETE ON speaking_guidance_audio FOR EACH ROW EXECUTE FUNCTION guard_guidance_audio()"
    )
    op.execute("""CREATE FUNCTION require_guidance_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.status='DELIVERED' AND (TG_OP='INSERT' OR OLD.status <> 'DELIVERED') AND NOT EXISTS (
        SELECT 1 FROM speaking_guidance_audio a JOIN playbacks p ON p.guidance_audio_id=a.audio_id
        WHERE a.help_request_id=NEW.help_request_id AND a.attempt_id=NEW.attempt_id
          AND a.status='READY' AND p.attempt_id=NEW.attempt_id AND p.status='COMPLETED'
          AND p.kind IN ('GUIDANCE','TECHNICAL_RETRY')
      ) THEN RAISE EXCEPTION 'delivered guidance requires completed playback'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER require_guidance_delivery BEFORE INSERT OR UPDATE ON speaking_help_requests FOR EACH ROW EXECUTE FUNCTION require_guidance_delivery()"
    )


def downgrade():
    op.execute("LOCK TABLE speaking_guidance_audio, playbacks IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM speaking_guidance_audio) THEN RAISE EXCEPTION 'guidance audio facts must be preserved'; END IF; END $$"
    )
    op.execute("DROP TRIGGER require_guidance_delivery ON speaking_help_requests")
    op.execute("DROP FUNCTION require_guidance_delivery()")
    op.drop_constraint("ck_playback_resource", "playbacks", type_="check")
    op.drop_constraint("ck_playback_kind", "playbacks", type_="check")
    op.create_check_constraint(
        "ck_playback_kind",
        "playbacks",
        "kind IN ('GUIDE','INITIAL_PROMPT','USER_REPLAY','TECHNICAL_RETRY')",
    )
    op.drop_column("playbacks", "guidance_audio_id")
    op.alter_column("playbacks", "audio_id", nullable=False)
    op.execute("""CREATE OR REPLACE FUNCTION guard_playback_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'playback facts must be preserved'; END IF;
      IF NOT EXISTS (SELECT 1 FROM attempts WHERE attempt_id=NEW.attempt_id AND status='OPEN') THEN RAISE EXCEPTION 'playback attempt is not open'; END IF;
      IF TG_OP='INSERT' THEN
        IF NEW.status <> 'STARTED' THEN RAISE EXCEPTION 'playback must start first'; END IF;
      ELSIF OLD.status <> 'STARTED' OR NEW.status NOT IN ('COMPLETED','FAILED') OR
        (NEW.playback_id,NEW.attempt_id,NEW.audio_id,NEW.request_id,NEW.kind,NEW.retry_of_id)
        IS DISTINCT FROM (OLD.playback_id,OLD.attempt_id,OLD.audio_id,OLD.request_id,OLD.kind,OLD.retry_of_id)
        THEN RAISE EXCEPTION 'playback fact is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.drop_table("speaking_guidance_audio")
    op.execute("DROP FUNCTION guard_guidance_audio()")
