"""Bind credential regions without rewriting historical ciphertext."""

import sqlalchemy as sa
from alembic import op

revision = "0026_llm_credential_region"
down_revision = "0025_llm_provider_config"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_provider_credentials", sa.Column("region", sa.Text(), nullable=True))
    # 0025's guard does not include the new column; existing immutable fields stay guarded.
    op.execute("""UPDATE llm_provider_credentials c SET region = x.region
      FROM (SELECT credential_id, min(region) AS region
            FROM llm_provider_configs
            WHERE provider = 'dashscope'
            GROUP BY credential_id
            HAVING count(*) = count(region) AND count(DISTINCT region) = 1
               AND min(region) IN ('cn-beijing','ap-southeast-1','us-east-1')) x
      WHERE c.credential_id = x.credential_id AND c.provider = 'dashscope'""")
    op.create_check_constraint(
        "ck_llm_credential_region",
        "llm_provider_credentials",
        "provider <> 'deepseek' OR region IS NULL",
    )
    op.execute("""CREATE OR REPLACE FUNCTION guard_llm_credential() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'credential history must be preserved'; END IF;
      IF (NEW.credential_id,NEW.provider,NEW.version,NEW.region,NEW.ciphertext,NEW.created_at) IS DISTINCT FROM
         (OLD.credential_id,OLD.provider,OLD.version,OLD.region,OLD.ciphertext,OLD.created_at)
         OR (NOT OLD.enabled AND NEW.enabled) THEN RAISE EXCEPTION 'credential identity is immutable'; END IF;
      RETURN NEW;
    END $$""")


def downgrade():
    op.execute("LOCK TABLE llm_provider_credentials IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM llm_provider_credentials) THEN
       RAISE EXCEPTION 'cannot discard credential region history'; END IF; END $$""")
    op.execute("""CREATE OR REPLACE FUNCTION guard_llm_credential() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'credential history must be preserved'; END IF;
      IF (NEW.credential_id,NEW.provider,NEW.version,NEW.ciphertext,NEW.created_at) IS DISTINCT FROM
         (OLD.credential_id,OLD.provider,OLD.version,OLD.ciphertext,OLD.created_at)
         OR (NOT OLD.enabled AND NEW.enabled) THEN RAISE EXCEPTION 'credential identity is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.drop_constraint("ck_llm_credential_region", "llm_provider_credentials", type_="check")
    op.drop_column("llm_provider_credentials", "region")
