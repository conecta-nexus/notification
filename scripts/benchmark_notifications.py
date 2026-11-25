"""Measure database execution time on a disposable, migrated PostgreSQL database."""

import asyncio
import json
import os
from math import ceil
from statistics import median
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.core.config import Settings
from src.core.database import create_engine

INDEX = "ix_notifications_user_id_is_read_created_at"
RECIPIENTS = (1, 10, 11, 500, 1010)
REPETITIONS = 30
PAGE_SIZE = 20
UUID_SQL = "('00000000-0000-4000-8000-' || lpad(recipient::text, 12, '0'))::uuid"
LOAD = text(f"""
    INSERT INTO notifications (user_id, event_type, title, message, is_read, created_at)
    SELECT {UUID_SQL}, 'APPLICATION_CREATED', 'Synthetic benchmark', 'Synthetic content',
           ordinal % 3 = 0, '2026-01-01T00:00:00Z'::timestamptz + ordinal * interval '1 second'
    FROM (
      SELECT ordinal, CASE WHEN ordinal <= 20000 THEN 1 + ordinal % 10
                           ELSE 11 + ordinal % 1000 END AS recipient
      FROM generate_series(1, 120000) AS ordinal
    ) AS fixture
""")
WHERE = "WHERE user_id = :recipient AND is_read = false"
LIST = text(f"""
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    SELECT id, event_type, title, message, link_url, is_read, created_at
    FROM notifications {WHERE} ORDER BY created_at DESC LIMIT :page_size
""")
COUNT = text(f"""
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    SELECT count(*) FROM notifications {WHERE}
""")


def uses_index(node):
    return node.get("Index Name") == INDEX or any(
        uses_index(child) for child in node.get("Plans", [])
    )


def summarize(samples):
    ordered = sorted(samples)
    return {
        "minimum": ordered[0],
        "median": median(ordered),
        "p95": ordered[ceil(len(ordered) * 0.95) - 1],
        "maximum": ordered[-1],
    }


async def benchmark():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "Provide TEST_DATABASE_URL for the disposable PostgreSQL database."
        )
    settings = Settings(_env_file=None, database_url=dsn)
    if (
        make_url(settings.database_url.get_secret_value()).database
        != "notification_verify"
    ):
        raise SystemExit(
            "The benchmark requires the disposable notification_verify database."
        )
    engine = create_engine(settings)
    results = []
    try:
        async with engine.connect() as conn, conn.begin() as transaction:
            database, version = (
                await conn.execute(
                    text("SELECT current_database(), current_setting('server_version')")
                )
            ).one()
            if database != "notification_verify" or not version.startswith("16."):
                raise SystemExit("The benchmark requires disposable PostgreSQL 16.")
            if await conn.scalar(text("SELECT count(*) FROM notifications")):
                raise SystemExit("The benchmark requires an empty notifications table.")
            await conn.execute(LOAD)
            await conn.execute(text("ANALYZE notifications"))
            rows, recipients, unread = (
                await conn.execute(
                    text("""
                SELECT count(*), count(DISTINCT user_id), count(*) FILTER (WHERE NOT is_read)
                FROM notifications
            """)
                )
            ).one()
            for recipient in RECIPIENTS:
                identifier = UUID(f"00000000-0000-4000-8000-{recipient:012d}")
                params = {"recipient": identifier, "page_size": PAGE_SIZE}
                total, unread_for_user = (
                    await conn.execute(
                        text("""
                    SELECT count(*), count(*) FILTER (WHERE NOT is_read)
                    FROM notifications WHERE user_id = :recipient
                """),
                        params,
                    )
                ).one()
                initial = (await conn.scalar(LIST, params))[0]
                warm = [
                    (await conn.scalar(LIST, params))[0] for _ in range(REPETITIONS)
                ]
                samples = [plan["Execution Time"] for plan in warm]
                results.append(
                    {
                        "recipient": str(identifier),
                        "rows": total,
                        "unread": unread_for_user,
                        "initial": initial,
                        "warm_plan": warm[-1],
                        "warm_execution_ms": summarize(samples),
                        "warm_samples_ms": samples,
                        "count_plan": (await conn.scalar(COUNT, params))[0],
                        "index_for_all_pages": all(
                            uses_index(plan["Plan"]) for plan in [initial, *warm]
                        ),
                    }
                )
            await transaction.rollback()
    finally:
        await engine.dispose()
    return {
        "postgresql": version,
        "rows": rows,
        "recipients": recipients,
        "unread": unread,
        "recipient_distribution": "10 recipients x 2000 records and 1000 recipients x 100 records",
        "conditions": "First measured query after fixture insert, ANALYZE and cardinality check; no cold-cache reset. Repeated queries reuse the same connection.",
        "page_size": PAGE_SIZE,
        "warm_repetitions_per_recipient": REPETITIONS,
        "unit": "PostgreSQL EXPLAIN execution time in milliseconds, excluding HTTP and client round-trip",
        "results": results,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(benchmark()), indent=2))
