from alembic import context

from app import (
    assessment_session,  # noqa: F401
    fixed_audio,  # noqa: F401
    import_batch,  # noqa: F401
    paper,  # noqa: F401
    submission,  # noqa: F401
    taskbank,  # noqa: F401
    tts_attempt,  # noqa: F401
)
from app.database import Base, create_database_engine

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
