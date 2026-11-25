import asyncio
import os
import subprocess
import sys
from time import monotonic
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.core.database import get_db, ping_database
from src.main import create_app

LOST_DATABASE = "postgresql://127.0.0.1:1/notification_test"


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_environment_precedence_and_supported_urls(monkeypatch, tmp_path, scheme):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DB_POOL_SIZE=2\n", encoding="ascii")
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("JWT_AUDIENCES", '["notification-test"]')
    monkeypatch.setenv("DATABASE_URL", f"{scheme}://test@localhost/notification_test")
    settings = Settings(_env_file=dotenv)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert (settings.db_pool_size, settings.db_max_overflow) == (3, 0)
    assert settings.jwt_audiences == ["notification-test"]


@pytest.mark.parametrize(
    "variable,value",
    [
        ("DATABASE_URL", ""),
        ("DATABASE_URL", "invalid-db-password"),
        ("DATABASE_URL", "sqlite:///invalid-db-password"),
        ("DATABASE_URL", "postgresql+psycopg://localhost/database"),
        ("DATABASE_URL", "postgresql://localhost"),
        ("DB_POOL_SIZE", "0"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DB_HEALTH_TIMEOUT", "300"),
        ("DB_CONNECT_TIMEOUT", "0"),
        ("DB_POOL_TIMEOUT", "-1"),
        ("JWT_AUDIENCES", '[" "]'),
        ("PORT", "70000"),
        ("APPLICATION_EVENT_SECRET", "too-short"),
    ],
)
def test_invalid_or_missing_settings_are_safe(monkeypatch, variable, value):
    monkeypatch.setenv("DATABASE_URL", LOST_DATABASE)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None)
    assert "invalid-db-password" not in str(failure.value)
    assert "too-short" not in str(failure.value)


def test_canonical_and_legacy_import_do_not_connect():
    program = """
from unittest.mock import patch
with patch('asyncpg.connect', side_effect=AssertionError('unexpected network operation')):
    from src.main import app
    from src.notification.main import app as legacy
    assert app is legacy
    print('OK')
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "OK"


def test_fastapi_starts_without_database_and_reports_unhealthy():
    settings = Settings(
        _env_file=None, database_url=LOST_DATABASE, db_health_timeout=0.2
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").json() == {
            "service": "notification",
            "status": "running",
            "framework": "fastapi",
        }
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "unhealthy", "database": "unavailable"}


@pytest.mark.asyncio
async def test_postgresql_pool_sessions_health_timeout_and_disposal():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires a disposable PostgreSQL instance")
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        db_pool_size=1,
        db_max_overflow=1,
        db_pool_timeout=5,
        db_health_timeout=0.15,
        db_connect_timeout=3,
    )
    app = create_app(settings)

    @app.get("/test-session")
    async def session_probe(
        session: Annotated[AsyncSession, Depends(get_db)], fail: bool = False
    ):
        database = await session.scalar(text("SELECT current_database()"))
        if fail:
            raise RuntimeError("Synthetic test failure")
        return {"database": database}

    async with app.router.lifespan_context(app):
        engine = app.state.engine
        pool = engine.pool
        assert engine.dialect.driver == "asyncpg"
        assert pool.size() == 1
        assert pool._max_overflow == 1
        assert pool._pre_ping is True
        async with AsyncClient(
            transport=ASGITransport(app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/test-session")
            assert response.json() == {"database": "notification_verify"}
            assert pool.checkedout() == 0
            assert (await client.get("/test-session?fail=true")).status_code == 500
            assert pool.checkedout() == 0
            assert await ping_database(engine, 2)
            async with engine.connect() as connection:
                assert (
                    int(await connection.scalar(text("SHOW server_version_num")))
                    // 10000
                    == 16
                )
                async with engine.connect():
                    assert pool.checkedout() == 2
                    start = monotonic()
                    assert (await client.get("/health")).status_code == 503
                    assert monotonic() - start < 1
            assert pool.checkedout() == 0
            assert (await client.get("/health")).json() == {
                "status": "healthy",
                "database": "connected",
            }
            await asyncio.sleep(0)
    assert engine.pool is not pool
    assert pool.checkedout() == pool.checkedin() == 0
