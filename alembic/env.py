import asyncio

from pydantic import ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import context
from src.core.config import Settings
from src.core.database import Base
from src.models.notification import Notification

target_metadata = Base.metadata
assert Notification.__table__.metadata is target_metadata


def migration_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        fields = ", ".join(
            str(item["loc"][0]) for item in error.errors(include_input=False)
        )
        raise RuntimeError(f"Invalid Notification settings: {fields}.") from None


def run_migrations(connection: Connection | None = None) -> None:
    if connection is None:
        context.configure(
            url=migration_settings().database_url.get_secret_value(),
            target_metadata=target_metadata,
            literal_binds=True,
        )
    else:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_server_default=True,
        )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = migration_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": settings.db_connect_timeout},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations()
else:
    asyncio.run(run_async_migrations())
