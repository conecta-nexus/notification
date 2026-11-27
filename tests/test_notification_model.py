import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.core.config import Settings
from src.core.database import Base, create_engine, create_session_factory
from src.models.notification import Notification

TABLE = "notifications"
INDEX = "ix_notifications_user_id_is_read_created_at"
TYPES = (
    "APPLICATION_CREATED",
    "APPLICATION_ACCEPTED",
    "APPLICATION_REJECTED",
    "CHALLENGE_CLOSED",
)
INSERT = text("""
    INSERT INTO notifications (user_id, event_type, title, message)
    VALUES (gen_random_uuid(), 'APPLICATION_CREATED', 'Synthetic', 'Test only')
    RETURNING id, is_read, created_at, link_url
""")


async def database_query(settings, query):
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            return (await connection.execute(query)).all()
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def migrated_database():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires a disposable PostgreSQL instance")
    settings = Settings(_env_file=None, database_url=dsn)
    assert (
        make_url(settings.database_url.get_secret_value()).database
        == "notification_verify"
    )
    assert asyncio.run(
        database_query(
            settings,
            text(
                "SELECT current_database(), current_setting('server_version_num')::int / 10000"
            ),
        )
    ) == [("notification_verify", 16)]
    assert (
        asyncio.run(
            database_query(
                settings,
                text("""
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    """),
            )
        )
        == []
    ), "Migration suite requires a freshly provisioned database"
    alembic(settings, "upgrade", "head")
    return settings


def alembic(settings, *args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, "DATABASE_URL": settings.database_url.get_secret_value()},
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_notification_migration_round_trip(migrated_database):
    config = migrated_database
    assert "001 (head)" in alembic(config, "current")
    assert "001 (head)" in alembic(config, "heads")
    assert "No new upgrade operations detected" in alembic(config, "check")
    identifier, read, created_at, link = asyncio.run(database_query(config, INSERT))[0]
    assert isinstance(identifier, UUID)
    assert read is False
    assert created_at.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - created_at) < timedelta(minutes=1)
    assert link is None
    alembic(config, "upgrade", "head")
    assert asyncio.run(
        database_query(
            config, text("SELECT id, is_read, created_at, link_url FROM notifications")
        )
    ) == [(identifier, read, created_at, link)]
    alembic(config, "downgrade", "base")
    assert asyncio.run(
        database_query(config, text("SELECT to_regclass('public.notifications')"))
    ) == [(None,)]
    alembic(config, "upgrade", "head")
    assert asyncio.run(
        database_query(config, text("SELECT count(*) FROM notifications"))
    ) == [(0,)]
    assert "No new upgrade operations detected" in alembic(config, "check")


@pytest.mark.asyncio
async def test_notification_postgresql_schema_and_index(migrated_database):
    assert Notification.__table__.metadata is Base.metadata
    assert Notification.__tablename__ == TABLE
    columns = await database_query(
        migrated_database,
        text("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'notifications'
    """),
    )
    assert {
        name: (kind, nullable, default) for name, kind, nullable, default in columns
    } == {
        "id": ("uuid", "NO", "gen_random_uuid()"),
        "user_id": ("uuid", "NO", None),
        "event_type": ("character varying", "NO", None),
        "title": ("character varying", "NO", None),
        "message": ("text", "NO", None),
        "link_url": ("character varying", "YES", None),
        "is_read": ("boolean", "NO", "false"),
        "created_at": ("timestamp with time zone", "NO", "now()"),
    }
    indexes = await database_query(
        migrated_database,
        text("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'notifications' ORDER BY indexname
    """),
    )
    assert indexes == [
        (
            INDEX,
            f"CREATE INDEX {INDEX} ON public.notifications USING btree (user_id, is_read, created_at)",
        ),
        (
            "notifications_pkey",
            "CREATE UNIQUE INDEX notifications_pkey ON public.notifications USING btree (id)",
        ),
    ]
    constraints = await database_query(
        migrated_database,
        text("""
        SELECT contype::text, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'public.notifications'::regclass AND contype IN ('p','f','c','u')
    """),
    )
    assert constraints == [("p", "PRIMARY KEY (id)")]


def notification(user_id, event_type=TYPES[0], **overrides):
    fields = {
        "user_id": user_id,
        "event_type": event_type,
        "title": "Synthetic notification",
        "message": "Test only",
    }
    return Notification(**(fields | overrides))


@pytest.mark.asyncio
async def test_notification_async_persistence_and_read_state(migrated_database):
    engine = create_engine(migrated_database)
    sessions = create_session_factory(engine)
    first, second = uuid4(), uuid4()
    try:
        async with sessions() as session:
            records = [notification(first, kind) for kind in TYPES]
            records.append(notification(second, link_url="/applications/synthetic"))
            session.add_all(records)
            await session.flush()
            assert all(
                isinstance(row.id, UUID) and row.is_read is False for row in records
            )
            assert len({row.id for row in records}) == len(records)
            assert all(row.created_at.utcoffset() == timedelta(0) for row in records)
            identifier, created_at = records[0].id, records[0].created_at
            await session.commit()
        async with sessions() as session:
            history = (
                await session.scalars(
                    select(Notification).where(Notification.user_id == first)
                )
            ).all()
            assert {row.event_type for row in history} == set(TYPES)
            assert len(history) == 4 and all(row.link_url is None for row in history)
            other = (
                await session.scalars(
                    select(Notification).where(Notification.user_id == second)
                )
            ).one()
            assert other.link_url == "/applications/synthetic"
            record = await session.get(Notification, identifier)
            record.is_read = True
            await session.commit()
        async with sessions() as session:
            record = await session.get(Notification, identifier)
            assert record.is_read is True and record.created_at == created_at
            session.expunge(record)
            with pytest.raises(IntegrityError) as duplicate:
                session.add(notification(first, id=identifier))
                await session.flush()
            assert duplicate.value.orig.sqlstate == "23505"
            await session.rollback()
            for field in ("user_id", "event_type", "title", "message"):
                with pytest.raises(IntegrityError) as missing:
                    session.add(
                        notification(first, **{field: None})
                        if field != "user_id"
                        else notification(None)
                    )
                    await session.flush()
                assert missing.value.orig.sqlstate == "23502"
                await session.rollback()
            rolled_back = notification(first)
            session.add(rolled_back)
            await session.flush()
            rolled_back_id = rolled_back.id
            await session.rollback()
        async with sessions() as session:
            assert await session.get(Notification, rolled_back_id) is None
            assert (await session.get(Notification, identifier)).is_read is True
            await session.execute(
                delete(Notification).where(Notification.user_id.in_([first, second]))
            )
            await session.commit()
    finally:
        await engine.dispose()
