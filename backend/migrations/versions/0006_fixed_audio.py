"""Freeze fixed audio requests and protect completed resources."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_fixed_audio"
down_revision = "0005_publication_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("LOCK TABLE papers, paper_entries IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM papers WHERE status <> 'DRAFT') THEN
        RAISE EXCEPTION 'existing published papers require explicit fixed audio reconciliation';
      END IF;
    END $$""")
    op.create_table(
        "fixed_audio_requests",
        sa.Column("audio_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "paper_entry_id",
            sa.Uuid(),
            sa.ForeignKey("paper_entries.paper_entry_id"),
            nullable=False,
        ),
        sa.Column(
            "task_revision_id",
            sa.Uuid(),
            sa.ForeignKey("task_revisions.task_revision_id"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("storage_ref", sa.Text()),
        sa.Column("media_type", sa.Text()),
        sa.Column("content_sha256", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("paper_entry_id", "role", name="uq_fixed_audio_entry_role"),
        sa.CheckConstraint("role IN ('guide', 'prompt')", name="ck_fixed_audio_role"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'FAILED', 'READY')", name="ck_fixed_audio_status"
        ),
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_fixed_audio_text_sha"),
        sa.CheckConstraint(
            "(status = 'READY' AND storage_ref IS NOT NULL AND media_type IS NOT NULL AND content_sha256 IS NOT NULL AND error IS NULL) OR (status <> 'READY' AND storage_ref IS NULL AND media_type IS NULL AND content_sha256 IS NULL)",
            name="ck_fixed_audio_result",
        ),
        sa.CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_fixed_audio_content_sha",
        ),
        sa.CheckConstraint("jsonb_typeof(config) = 'object'", name="ck_fixed_audio_config"),
    )
    op.execute("""CREATE FUNCTION guard_fixed_audio() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target_id uuid; rev uuid; p_status text; guide text; prompt text;
    BEGIN
      IF TG_OP = 'DELETE' THEN target_id := OLD.paper_entry_id; ELSE target_id := NEW.paper_entry_id; END IF;
      IF TG_OP = 'UPDATE' AND (NEW.audio_id, NEW.paper_entry_id, NEW.task_revision_id, NEW.role, NEW.source_text, NEW.text_sha256, NEW.config)
        IS DISTINCT FROM (OLD.audio_id, OLD.paper_entry_id, OLD.task_revision_id, OLD.role, OLD.source_text, OLD.text_sha256, OLD.config) THEN
        RAISE EXCEPTION 'fixed audio source and configuration are immutable';
      END IF;
      SELECT e.task_revision_id, p.status, r.guide_text, r.prompt_text INTO rev, p_status, guide, prompt
      FROM paper_entries e JOIN papers p ON p.paper_id=e.paper_id JOIN task_revisions r ON r.task_revision_id=e.task_revision_id
      WHERE e.paper_entry_id=target_id FOR UPDATE OF p;
      IF p_status IS NULL THEN RAISE EXCEPTION 'missing paper entry'; END IF;
      IF TG_OP = 'DELETE' THEN
        IF p_status <> 'DRAFT' THEN RAISE EXCEPTION 'published audio request is immutable'; END IF;
        RETURN OLD;
      END IF;
      IF TG_OP = 'INSERT' AND (p_status <> 'DRAFT' OR NEW.status <> 'PENDING') THEN RAISE EXCEPTION 'audio must be frozen before publication'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.status = 'READY' THEN RAISE EXCEPTION 'completed audio is immutable'; END IF;
      IF NEW.text_sha256 <> encode(sha256(convert_to(NEW.source_text, 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'fixed audio text digest mismatch';
      END IF;
      IF coalesce(jsonb_typeof(NEW.config->'provider'),'') <> 'string' OR coalesce(NEW.config->>'provider','') = ''
         OR coalesce(jsonb_typeof(NEW.config->'model'),'') <> 'string' OR coalesce(NEW.config->>'model','') = ''
         OR coalesce(jsonb_typeof(NEW.config->'voice'),'') <> 'string' OR coalesce(NEW.config->>'voice','') = ''
         OR coalesce(jsonb_typeof(NEW.config->'version'),'') <> 'string' OR coalesce(NEW.config->>'version','') = ''
         OR coalesce(jsonb_typeof(NEW.config->'speed'),'') <> 'number' THEN
        RAISE EXCEPTION 'explicit fixed audio configuration is required';
      END IF;
      IF (NEW.config->>'speed')::numeric <= 0 THEN RAISE EXCEPTION 'invalid fixed audio speed'; END IF;
      IF NEW.status='READY' AND (NEW.media_type NOT LIKE 'audio/%' OR NEW.storage_ref <> 'sha256/' || substring(NEW.content_sha256,1,2) || '/' || NEW.content_sha256) THEN
        RAISE EXCEPTION 'invalid fixed audio resource';
      END IF;
      IF NEW.task_revision_id IS DISTINCT FROM rev OR
         (NEW.role='guide' AND (guide IS NULL OR guide='' OR NEW.source_text IS DISTINCT FROM guide)) OR
         (NEW.role='prompt' AND NEW.source_text IS DISTINCT FROM prompt) THEN
        RAISE EXCEPTION 'fixed audio source must match revision';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER fixed_audio_guard BEFORE INSERT OR UPDATE OR DELETE ON fixed_audio_requests FOR EACH ROW EXECUTE FUNCTION guard_fixed_audio()"
    )
    op.execute("""CREATE FUNCTION guard_paper_audio_publication() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.status IN ('PUBLISHED', 'ARCHIVED') AND EXISTS (
        SELECT 1 FROM paper_entries e JOIN task_revisions r ON r.task_revision_id=e.task_revision_id
        WHERE e.paper_id=NEW.paper_id AND (
          NOT EXISTS (SELECT 1 FROM fixed_audio_requests a WHERE a.paper_entry_id=e.paper_entry_id AND a.role='prompt' AND a.task_revision_id=e.task_revision_id AND a.source_text=r.prompt_text)
          OR (r.guide_text IS NOT NULL AND r.guide_text<>'' AND NOT EXISTS (SELECT 1 FROM fixed_audio_requests a WHERE a.paper_entry_id=e.paper_entry_id AND a.role='guide' AND a.task_revision_id=e.task_revision_id AND a.source_text=r.guide_text))
        )
      ) THEN RAISE EXCEPTION 'published paper requires fixed audio requests'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER paper_audio_publication_guard BEFORE INSERT OR UPDATE ON papers FOR EACH ROW EXECUTE FUNCTION guard_paper_audio_publication()"
    )
    op.execute("""CREATE FUNCTION guard_entry_audio_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.task_revision_id IS DISTINCT FROM OLD.task_revision_id AND EXISTS (SELECT 1 FROM fixed_audio_requests WHERE paper_entry_id=OLD.paper_entry_id) THEN
        RAISE EXCEPTION 'remove draft audio requests before replacing entry';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER entry_audio_revision_guard BEFORE UPDATE ON paper_entries FOR EACH ROW EXECUTE FUNCTION guard_entry_audio_revision()"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE papers, paper_entries, fixed_audio_requests IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM fixed_audio_requests) OR EXISTS (SELECT 1 FROM papers WHERE status IN ('PUBLISHED', 'ARCHIVED')) THEN
        RAISE EXCEPTION 'cannot downgrade: fixed audio facts must be preserved';
      END IF;
    END $$""")
    op.execute("DROP TRIGGER entry_audio_revision_guard ON paper_entries")
    op.execute("DROP FUNCTION guard_entry_audio_revision()")
    op.execute("DROP TRIGGER paper_audio_publication_guard ON papers")
    op.execute("DROP FUNCTION guard_paper_audio_publication()")
    op.execute("DROP TRIGGER fixed_audio_guard ON fixed_audio_requests")
    op.execute("DROP FUNCTION guard_fixed_audio()")
    op.drop_table("fixed_audio_requests")
