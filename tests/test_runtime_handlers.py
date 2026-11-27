from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
import pytest

from src.core.config import Settings
from src.main import create_app
from src.models.notification import Notification
from src.schemas.events import (
    AcceptedEvent,
    ApplicationAccepted,
    ApplicationCreated,
    ApplicationRejected,
    ChallengeClosed,
    ClosedEvent,
    CreatedEvent,
    RejectedEvent,
)
from src.services.runtime_handlers import build_runtime_dispatcher


def _test_settings(**overrides) -> Settings:
    key = generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return Settings(
        _env_file=None,
        database_url="postgresql://127.0.0.1:1/notification_test",
        jwt_public_key=pem,
        application_event_secret="synthetic-application-credential-0000",
        challenge_event_secret="synthetic-challenge-credential-0000",
        **overrides,
    )


def _fake_session_factory():
    added: list[Notification] = []
    session = MagicMock()
    session.add_all = MagicMock(side_effect=added.extend)

    @asynccontextmanager
    async def begin_ctx():
        yield

    @asynccontextmanager
    async def session_ctx():
        yield session

    session.begin = begin_ctx
    return session_ctx, added


@pytest.mark.asyncio
async def test_runtime_dispatcher_persists_notifications_and_sends_emails():
    settings = _test_settings(
        smtp_host="mailpit",
        smtp_port=1025,
        smtp_from_email="noreply@conectanexus.ufps.edu.co",
        frontend_base_url="http://localhost:8080",
    )
    session_factory, added = _fake_session_factory()
    mailer = MagicMock()
    mailer.send = AsyncMock()
    dispatcher = build_runtime_dispatcher(settings, session_factory, email_dispatcher=mailer)

    student_id = uuid4()
    company_id = uuid4()
    app_id = uuid4()
    challenge_id = uuid4()

    await dispatcher.dispatch(
        CreatedEvent(
            type="APPLICATION_CREATED",
            payload=ApplicationCreated(
                student_id=student_id, challenge_id=challenge_id, company_id=company_id
            ),
        )
    )
    await dispatcher.dispatch(
        AcceptedEvent(
            type="APPLICATION_ACCEPTED",
            payload=ApplicationAccepted(
                application_id=app_id,
                student_id=student_id,
                company_id=company_id,
                contacts={
                    "student_email": "student@ufps.edu.co",
                    "student_name": "Ana Perez",
                    "company_email": "talent@acme.co",
                    "company_name": "Acme SAS",
                    "challenge_title": "AI Pipeline",
                },
            ),
        )
    )
    await dispatcher.dispatch(
        RejectedEvent(
            type="APPLICATION_REJECTED",
            payload=ApplicationRejected(
                application_id=app_id,
                student_id=student_id,
                feedback_reason="Missing required Python background.",
            ),
        )
    )
    await dispatcher.dispatch(
        ClosedEvent(type="CHALLENGE_CLOSED", payload=ChallengeClosed(challenge_id=challenge_id))
    )

    assert len(added) == 4
    assert added[0].user_id == company_id
    assert added[0].event_type == "APPLICATION_CREATED"
    assert added[1].user_id == student_id
    assert added[2].user_id == company_id
    assert added[3].user_id == student_id
    assert added[3].event_type == "APPLICATION_REJECTED"
    assert mailer.send.await_count == 2


def test_create_app_with_default_handlers_wires_runtime_dispatcher(monkeypatch):
    settings = _test_settings()
    with TestClient(create_app(settings, default_handlers=True)) as client:
        response = client.post(
            "/api/v1/notifications/events",
            json={"type": "CHALLENGE_CLOSED", "payload": {"challenge_id": str(uuid4())}},
            headers={
                "X-Event-Producer": "challenge",
                "X-Event-Producer-Token": "synthetic-challenge-credential-0000",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"status": "handled", "type": "CHALLENGE_CLOSED"}
