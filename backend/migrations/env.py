from alembic import context

from app import taskbank  # noqa: F401  Registers mapped tables for Alembic.
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
