from alembic import context

from app.database import Base, create_database_engine
from app.evaluation import models as evaluation_models  # noqa: F401
from app.evaluation import speaking_asr_models  # noqa: F401
from app.media import fixed_audio, recording, tts_attempt  # noqa: F401
from app.papers import paper  # noqa: F401
from app.scoring import models as scoring_models  # noqa: F401
from app.sessions import assessment_session, close_manifest, credentials, submission  # noqa: F401
from app.task_types import speaking, speaking_asr, speaking_guidance  # noqa: F401
from app.taskbank import import_batch, models  # noqa: F401

config = context.config
target_metadata = Base.metadata


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not configured")
else:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        run_migrations(supplied_connection)
    else:
        engine = create_database_engine()
        try:
            with engine.connect() as connection:
                run_migrations(connection)
        finally:
            engine.dispose()
