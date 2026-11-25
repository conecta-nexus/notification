import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from time import time
from uuid import UUID, uuid4

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url

from src.core.config import Settings
from src.core.database import create_engine
from src.main import create_app
from src.models.notification import Notification

BASE = "/api/v1/notifications"
ME = BASE + "/me"
ALL = BASE + "/read-all"


def read(identifier):
    return f"{BASE}/{identifier}/read"


@pytest.fixture(scope="module")
def key():
    return generate_private_key(public_exponent=65537, key_size=2048)


def authorization(key, user, role="STUDENT", **claims):
    now = int(time())
    payload = {
        "sub": str(user),
        "jti": str(uuid4()),
        "email": "synthetic@example.org",
        "iat": now,
        "nbf": now,
        "exp": now + 120,
        "type": "access",
        "role": role,
        "is_active": True,
        "scope": "",
        "iss": "https://auth.conectanexus.ufps.edu.co",
        "aud": ["conecta-nexus-gateway", "conecta-nexus-services"],
    }
    return {"Authorization": "Bearer " + jwt.encode(payload | claims, key, "RS256")}


def settings(key, dsn="postgresql://127.0.0.1:1/notification_verify", **overrides):
    pem = (
        key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return Settings(
        _env_file=None,
        **(
            {
                "database_url": dsn,
                "jwt_public_key": pem,
                "db_connect_timeout": 1,
                "application_event_secret": "synthetic-producer-credential-only",
            }
            | overrides
        ),
    )


@pytest.fixture(scope="module")
def disposable_dsn(key):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Requires a disposable PostgreSQL instance")
    config = settings(key, dsn)
    url = config.database_url.get_secret_value()
    assert make_url(url).database == "notification_verify"

    async def verify_database():
        engine = create_engine(config)
        try:
            async with engine.connect() as conn:
                assert (
                    await conn.execute(
                        text(
                            "SELECT current_database(), current_setting('server_version_num')::int / 10000"
                        )
                    )
                ).one() == ("notification_verify", 16)
        finally:
            await engine.dispose()

    asyncio.run(verify_database())
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return dsn


@pytest_asyncio.fixture
async def runtime(key, disposable_dsn):
    app = create_app(settings(key, disposable_dsn))
    users = [uuid4() for _ in range(3)]
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://notification.test"
        ) as client:
            yield client, app.state.session_factory, users
        async with app.state.session_factory.begin() as session:
            await session.execute(
                delete(Notification).where(Notification.user_id.in_(users))
            )
        assert app.state.engine.pool.checkedout() == 0


async def fixture_rows(sessions, first, second):
    stamp = datetime.now(UTC).replace(microsecond=0)
    async with sessions.begin() as session:
        rows = [
            Notification(
                user_id=first,
                title="Postulación",
                message="Diseño y análisis",
                event_type="FUTURE_EVENT",
                link_url="/challenges",
                created_at=stamp,
                is_read=False,
            ),
            Notification(
                user_id=first,
                title="Igual",
                message="Contenido",
                event_type="APPLICATION_CREATED",
                link_url="//external.example.org",
                created_at=stamp,
                is_read=False,
            ),
            Notification(
                user_id=first,
                title="Anterior",
                message="Contenido",
                event_type="APPLICATION_ACCEPTED",
                created_at=stamp - timedelta(seconds=1),
                is_read=True,
            ),
            Notification(
                user_id=second,
                title="Ajena",
                message="Private fixture",
                event_type="APPLICATION_REJECTED",
                is_read=False,
            ),
        ]
        session.add_all(rows)
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["STUDENT", "COMPANY", "ADMIN"])
async def test_role_owned_listing_pagination_and_foreign_updates(runtime, key, role):
    client, sessions, (owner, other, empty) = runtime
    first, equal, old, foreign = await fixture_rows(sessions, owner, other)
    headers = authorization(key, owner, role) | {
        "X-User-Id": str(other),
        "X-User-Role": "ADMIN",
    }
    response = await client.get(ME, headers=headers, params={"user_id": other})
    assert (
        response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    )
    data = response.json()
    ordered = [
        str(identifier) for identifier in sorted([first.id, equal.id], reverse=True)
    ] + [str(old.id)]
    assert [item["id"] for item in data["items"]] == ordered and data[
        "unread_count"
    ] == 2
    by_id = {item["id"]: item for item in data["items"]}
    assert by_id[str(first.id)] == {
        "id": str(first.id),
        "title": "Postulación",
        "message": "Diseño y análisis",
        "event_type": "FUTURE_EVENT",
        "link_url": "/challenges",
        "is_read": False,
        "created_at": first.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert (
        by_id[str(equal.id)]["link_url"] is None
        and by_id[str(old.id)]["link_url"] is None
    )
    for offset in range(4):
        page = (
            await client.get(ME, headers=headers, params={"limit": 1, "offset": offset})
        ).json()
        assert [item["id"] for item in page["items"]] == ordered[offset : offset + 1]
        assert page["unread_count"] == 2
    for index in (foreign.id, uuid4()):
        denied = await client.patch(read(index), headers=headers)
        assert (denied.status_code, denied.json()) == (
            404,
            {"detail": "Notification not found."},
        )
    assert (await client.get(ME, headers=authorization(key, empty, role))).json() == {
        "items": [],
        "unread_count": 0,
    }
    assert (
        await client.patch(ALL, headers=authorization(key, empty, role))
    ).json() == {"updated_count": 0, "unread_count": 0}


@pytest.mark.asyncio
async def test_read_and_bulk_persist_without_changing_other_columns(runtime, key):
    client, sessions, (owner, other, _) = runtime
    first, equal, old, foreign = await fixture_rows(sessions, owner, other)
    headers = authorization(key, owner)
    malicious = await client.patch(
        read(first.id),
        json={"user_id": str(other), "title": "mismatch"},
        headers=headers,
    )
    assert malicious.status_code == 422 and malicious.json() == {
        "detail": "Invalid request payload."
    }
    assert (
        await client.patch(ALL, json={"is_read": False}, headers=headers)
    ).status_code == 422
    for identifier, count in [(first.id, 1), (first.id, 1), (old.id, 1)]:
        response = await client.patch(read(identifier), headers=headers)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "id": str(identifier),
            "is_read": True,
            "unread_count": count,
        }
    async with sessions() as session:
        row = await session.get(Notification, first.id)
        assert row.is_read and row.created_at == first.created_at
        assert (row.user_id, row.title, row.message, row.event_type, row.link_url) == (
            first.user_id,
            first.title,
            first.message,
            first.event_type,
            first.link_url,
        )
    for updated in (1, 0):
        response = await client.patch(ALL, json={}, headers=headers)
        assert response.json() == {"updated_count": updated, "unread_count": 0}
    async with sessions() as session:
        assert (await session.get(Notification, equal.id)).is_read
        assert not (await session.get(Notification, foreign.id)).is_read


@pytest.mark.asyncio
async def test_postgres_concurrent_reads_and_bulk(runtime, key):
    client, sessions, (owner, other, _) = runtime
    first, equal, old, foreign = await fixture_rows(sessions, owner, other)
    headers = authorization(key, owner)
    responses = await asyncio.gather(
        *[
            client.patch(path, headers=headers)
            for path in [read(first.id), read(first.id), ALL, read(equal.id), ALL]
        ]
    )
    assert all(
        response.status_code == 200 and response.json()["unread_count"] >= 0
        for response in responses
    )
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(Notification).where(Notification.user_id == owner)
            )
        ).all()
        assert {row.id: row.created_at for row in rows} == {
            row.id: row.created_at for row in [first, equal, old]
        }
        assert all(row.is_read for row in rows)
        assert not (await session.get(Notification, foreign.id)).is_read


@pytest.mark.asyncio
async def test_commit_failure_is_sanitized_and_rolls_back(runtime, key):
    client, sessions, (owner, other, _) = runtime
    first, _, _, _ = await fixture_rows(sessions, owner, other)
    async with sessions.begin() as session:
        await session.execute(
            text("""
            CREATE FUNCTION verify_fail_notification_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private synthetic SQL text' USING ERRCODE='23514'; END;
            $$;
        """)
        )
        await session.execute(
            text("""
            CREATE CONSTRAINT TRIGGER verify_fail_notification_write AFTER UPDATE ON notifications
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION verify_fail_notification_write();
        """)
        )
    try:
        response = await client.patch(read(first.id), headers=authorization(key, owner))
        assert (response.status_code, response.json()) == (
            500,
            {"detail": "Could not complete notification operation."},
        )
        async with sessions() as session:
            assert not (await session.get(Notification, first.id)).is_read
    finally:
        async with sessions.begin() as session:
            await session.execute(
                text("DROP TRIGGER verify_fail_notification_write ON notifications")
            )
            await session.execute(
                text("DROP FUNCTION verify_fail_notification_write()")
            )


@pytest.mark.asyncio
async def test_authentication_rejects_alternative_credentials_before_query(key):
    user = uuid4()
    invalid = [
        {},
        {
            "X-User-Id": str(user),
            "X-User-Role": "ADMIN",
            "X-User-Email": "synthetic@example.org",
        },
        {
            "X-Event-Producer": "application",
            "X-Event-Producer-Token": "synthetic-producer-credential-only",
        },
        authorization(generate_private_key(public_exponent=65537, key_size=2048), user),
    ]
    for claims in [
        {"exp": int(time()) - 50},
        {"nbf": int(time()) + 50},
        {"sub": "invalid"},
        {"type": "refresh"},
        {"type": "password_reset"},
        {"is_active": False},
        {"iss": "invalid"},
        {"aud": ["invalid"]},
    ]:
        invalid.append(authorization(key, user, **claims))
    app = create_app(settings(key))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app), base_url="http://notification.test"
        ) as client,
    ):
        for headers in invalid:
            replies = [
                await client.get(ME, headers=headers),
                await client.patch(ALL, headers=headers),
                await client.patch(read(uuid4()), headers=headers),
            ]
            assert all(
                reply.status_code == 401
                and reply.headers["WWW-Authenticate"] == "Bearer"
                for reply in replies
            )


@pytest.mark.asyncio
async def test_missing_key_invalid_input_and_disconnected_storage(key, tmp_path):
    user = uuid4()
    headers = authorization(key, user)
    no_key = create_app(
        settings(key, jwt_public_key="", jwt_public_key_path=tmp_path / "missing.pem")
    )
    async with (
        no_key.router.lifespan_context(no_key),
        AsyncClient(transport=ASGITransport(no_key), base_url="http://test") as client,
    ):
        assert (await client.get(ME, headers=headers)).status_code == 401
    app = create_app(settings(key))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        invalid_queries = [
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"offset": 2147483648},
            {"limit": "text"},
        ]
        for params in invalid_queries:
            assert (
                await client.get(ME, headers=headers, params=params)
            ).status_code == 422
        assert (
            await client.patch(read("not-uuid"), headers=headers)
        ).status_code == 422
        for method, path in [("GET", ME), ("PATCH", ALL), ("PATCH", read(uuid4()))]:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "Notification storage temporarily unavailable."
            }
            assert response.headers["Cache-Control"] == "no-store"
        assert app.state.engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    "unsafe",
    [
        "https://elsewhere.example.org",
        "javascript:alert(1)",
        "data:text/html,x",
        "//external",
        "/%252fexternal",
        "/\\external",
        "/a/../b",
        "/auth/login?next=//other",
        "/\x00path",
    ],
)
def test_unsafe_navigation_is_null(unsafe):
    from src.schemas.notifications import NotificationItem

    item = NotificationItem(
        id=UUID(int=0),
        event_type="CUSTOM",
        title="t",
        message="m",
        created_at=datetime.now(UTC),
        is_read=False,
        link_url=unsafe,
    )
    assert item.link_url is None


def test_openapi_models_auth_optional_body_and_uuid():
    document = create_app().openapi()
    operations = {
        path: {method: data for method, data in methods.items()}
        for path, methods in document["paths"].items()
        if path.startswith(BASE) and not path.endswith("/events")
    }
    assert {path: set(methods) for path, methods in operations.items()} == {
        ME: {"get"},
        ALL: {"patch"},
        BASE + "/{notification_id}/read": {"patch"},
    }
    for methods in operations.values():
        for operation in methods.values():
            assert operation["security"] == [{"HTTPBearer": []}]
            assert {"200", "401", "422", "503"} <= operation["responses"].keys()
    patch = operations[BASE + "/{notification_id}/read"]["patch"]
    assert patch["parameters"][0]["schema"]["format"] == "uuid"
    assert (
        not patch["requestBody"].get("required", False) and "404" in patch["responses"]
    )
    assert (
        document["components"]["schemas"]["MarkReadRequest"]["additionalProperties"]
        is False
    )
