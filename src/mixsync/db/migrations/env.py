from alembic import context

from mixsync.core.config import Settings
from mixsync.db.base import Base
from mixsync.db.engine import make_engine

target_metadata = Base.metadata
settings = Settings()

if context.is_offline_mode():
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    with make_engine(settings).connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
