import asyncio
from time import time
from typing import Annotated
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.core.config import Settings
from src.core.security import Principal, authenticated_user, load_verification_key
from src.main import create_app
from src.services.event_dispatcher import EventDispatcher

URL = "/api/v1/notifications/events"
KEYS = {
    "application": "synthetic-application-credential-0000",
    "challenge": "synthetic-challenge-credential-0000",
}
ID = str(uuid4())
EVENTS = {
    "APPLICATION_CREATED": {"student_id": ID, "challenge_id": ID, "company_id": ID},
    "APPLICATION_ACCEPTED": {
        "application_id": ID,
        "student_id": ID,
        "company_id": ID,
        "contacts": {"channel": "synthetic"},
    },
    "APPLICATION_REJECTED": {
        "application_id": ID,
        "student_id": ID,
        "feedback_reason": "Synthetic feedback",
    },
    "CHALLENGE_CLOSED": {"challenge_id": ID},
}


@pytest.fixture(scope="module")
def signer():
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(signer):
    pem = signer.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return Settings(
        _env_file=None,
        database_url="postgresql://127.0.0.1:1/notification_test",
        jwt_public_key=pem.decode().replace("\n", "\\n"),
        application_event_secret=KEYS["application"],
        challenge_event_secret=KEYS["challenge"],
    )


def claims(**overrides):
    now = int(time())
    return {
        "sub": ID,
        "jti": str(uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 90,
        "type": "access",
        "email": "synthetic@example.org",
        "role": "STUDENT",
        "is_active": True,
        "scope": "challenge:read application:create",
        "iss": "https://auth.conectanexus.ufps.edu.co",
        "aud": ["conecta-nexus-gateway", "conecta-nexus-services"],
        **overrides,
    }


def user_app(settings):
    app = create_app(settings)

    @app.get("/test-principal")
    def probe(principal: Annotated[Principal, Depends(authenticated_user)]):
        return {
            "id": str(principal.id),
            "role": principal.role,
            "scopes": sorted(principal.scopes),
        }

    return app


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def event_headers(producer):
    return {"X-Event-Producer": producer, "X-Event-Producer-Token": KEYS[producer]}


@pytest.mark.parametrize("inline_override", [False, True])
def test_runtime_pem_override_selects_expected_signer(
    monkeypatch, tmp_path, signer, inline_override
):
    old_signer = generate_private_key(public_exponent=65537, key_size=2048)
    mounted = tmp_path / "public.pem"
    mounted.write_bytes(
        signer.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
    )
    old_pem = old_signer.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DATABASE_URL=postgresql://localhost/notification_test\n"
        f'JWT_PUBLIC_KEY="{old_pem.decode().strip()}"\n',
        encoding="ascii",
    )
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(mounted))
    monkeypatch.setenv("JWT_PUBLIC_KEY", old_pem.decode() if inline_override else "")
    settings = Settings(_env_file=dotenv)
    selected, rejected = (
        (old_signer, signer) if inline_override else (signer, old_signer)
    )
    assert (
        load_verification_key(settings).public_numbers()
        == selected.public_key().public_numbers()
    )
    with TestClient(user_app(settings)) as client:
        assert (
            client.get(
                "/test-principal",
                headers=bearer(jwt.encode(claims(), selected, "RS256")),
            ).status_code
            == 200
        )
        response = client.get(
            "/test-principal", headers=bearer(jwt.encode(claims(), rejected, "RS256"))
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("disabled", ["application", "challenge"])
def test_empty_runtime_secret_disables_only_its_producer(
    monkeypatch, tmp_path, disabled
):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DATABASE_URL=postgresql://localhost/notification_test\n"
        + "".join(
            f"{name.upper()}_EVENT_SECRET={secret}\n" for name, secret in KEYS.items()
        ),
        encoding="ascii",
    )
    for name in KEYS:
        monkeypatch.delenv(f"{name.upper()}_EVENT_SECRET", raising=False)
    monkeypatch.setenv(f"{disabled.upper()}_EVENT_SECRET", "")
    config = Settings(_env_file=dotenv)

    observed = []

    async def handle(event):
        observed.append(event.type)

    kinds = {"application": "APPLICATION_CREATED", "challenge": "CHALLENGE_CLOSED"}
    app = create_app(config, EventDispatcher(dict.fromkeys(kinds.values(), handle)))
    with TestClient(app) as client:
        for producer, kind in kinds.items():
            response = client.post(
                URL,
                json={"type": kind, "payload": EVENTS[kind]},
                headers=event_headers(producer),
            )
            assert response.status_code == (401 if producer == disabled else 200)
            for secret in KEYS.values():
                assert secret not in response.text
    assert observed == [
        kind for producer, kind in kinds.items() if producer != disabled
    ]


def test_empty_optional_producer_secrets_from_example(monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(f"{name.upper()}_EVENT_SECRET", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/notification_test")
    settings = Settings(_env_file=".env.example")
    assert settings.application_event_secret is None
    assert settings.challenge_event_secret is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("DATABASE_URL", ""),
        ("APPLICATION_EVENT_SECRET", "synthetic-short"),
        ("CHALLENGE_EVENT_SECRET", "synthetic-short"),
    ],
)
def test_invalid_runtime_values_do_not_fall_back_to_dotenv(
    monkeypatch, tmp_path, field, value
):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DATABASE_URL=postgresql://test:synthetic-old-db-secret@localhost/notification_test\n"
        + "".join(
            f"{name.upper()}_EVENT_SECRET={secret}\n" for name, secret in KEYS.items()
        ),
        encoding="ascii",
    )
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=dotenv)
    assert failure.value.errors(include_input=False)[0]["loc"] == (field.lower(),)
    rendered = str(failure.value)
    for secret in [*KEYS.values(), "synthetic-old-db-secret", "synthetic-short"]:
        assert secret not in rendered


@pytest.mark.parametrize("role", ["STUDENT", "COMPANY", "ADMIN"])
def test_signed_user_principal(settings, signer, role):
    with TestClient(user_app(settings)) as client:
        response = client.get(
            "/test-principal",
            headers=bearer(jwt.encode(claims(role=role), signer, "RS256")),
        )
        assert response.status_code == 200
        assert response.json() == {
            "id": ID,
            "role": role,
            "scopes": ["application:create", "challenge:read"],
        }


def test_invalid_user_credentials(settings, signer):
    wrong_key = generate_private_key(public_exponent=65537, key_size=2048)
    expired = int(time()) - 10
    future = int(time()) + 1000
    signed = [
        jwt.encode(claims(**changes), signer, "RS256")
        for changes in (
            {"iss": "wrong"},
            {"aud": ["wrong"]},
            {"aud": "conecta-nexus-services"},
            {"exp": expired},
            {"exp": float(future)},
            {"nbf": future},
            {"iat": future},
            {"sub": "bad"},
            {"jti": "bad"},
            {"scope": []},
            {"email": []},
            {"role": "OTHER"},
            {"is_active": False},
            {"is_active": 1},
            {"type": "refresh"},
            {"type": "password_reset"},
        )
    ]
    base = claims()
    signed.extend(
        jwt.encode({k: v for k, v in base.items() if k != key}, signer, "RS256")
        for key in base
    )
    signed.extend(
        [
            jwt.encode(base, wrong_key, "RS256"),
            jwt.encode(base, signer, "RS384"),
            jwt.encode(base, None, "none"),
            "malformed",
        ]
    )
    with TestClient(user_app(settings)) as client:
        for headers in [
            {},
            {"Authorization": "Bearer"},
            {"Authorization": "Basic bad"},
            {"X-User-Role": "ADMIN"},
            *map(bearer, signed),
        ]:
            response = client.get("/test-principal", headers=headers)
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("key", ["missing", "invalid"])
def test_verification_keys_fail_closed(settings, signer, tmp_path, key):
    path = tmp_path / "public.pem"
    if key == "invalid":
        path.write_text("invalid", encoding="ascii")
    config = settings.model_copy(
        update={"jwt_public_key": "", "jwt_public_key_path": path}
    )
    with TestClient(user_app(config)) as client:
        assert (
            client.get(
                "/test-principal", headers=bearer(jwt.encode(claims(), signer, "RS256"))
            ).status_code
            == 401
        )


def test_all_events_are_dispatched_to_the_matching_async_handler(settings):
    seen = []

    def for_type(kind):
        async def handle(event):
            await asyncio.sleep(0)
            assert event.type == kind
            seen.append(kind)

        return handle

    dispatcher = EventDispatcher({kind: for_type(kind) for kind in EVENTS})
    with TestClient(create_app(settings, dispatcher)) as client:
        for kind, payload in EVENTS.items():
            producer = "challenge" if kind == "CHALLENGE_CLOSED" else "application"
            response = client.post(
                URL,
                json={"type": kind, "payload": payload},
                headers=event_headers(producer),
            )
            assert response.status_code == 200
            assert response.json() == {"status": "handled", "type": kind}
    assert seen == list(EVENTS)


def test_event_listener_rejects_invalid_contracts_and_unauthorized_producers(
    settings, signer
):
    event = {"type": "APPLICATION_CREATED", "payload": EVENTS["APPLICATION_CREATED"]}
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                URL,
                json=event,
                headers=bearer(jwt.encode(claims(role="ADMIN"), signer, "RS256")),
            ).status_code
            == 401
        )
        assert (
            client.post(URL, json=event, headers=event_headers("challenge")).status_code
            == 403
        )
        for body in [
            {"type": "PRIVATE_BODY_VALUE", "payload": {}},
            {
                "type": "APPLICATION_CREATED",
                "payload": {"student_id": "PRIVATE_BODY_VALUE"},
            },
            {
                "type": "APPLICATION_ACCEPTED",
                "payload": {
                    **EVENTS["APPLICATION_ACCEPTED"],
                    "contacts": "PRIVATE_BODY_VALUE",
                },
            },
            {
                "type": "APPLICATION_REJECTED",
                "payload": {**EVENTS["APPLICATION_REJECTED"], "feedback_reason": "  "},
            },
        ]:
            response = client.post(URL, json=body, headers=event_headers("application"))
            assert response.status_code == 422
            assert "PRIVATE_BODY_VALUE" not in response.text
        assert client.post(
            URL, json=event, headers=event_headers("application")
        ).json() == {"detail": "No event handler is available."}
    disabled = settings.model_copy(
        update={"application_event_secret": None, "challenge_event_secret": None}
    )
    with TestClient(create_app(disabled)) as client:
        assert (
            client.post(
                URL, json=event, headers=event_headers("application")
            ).status_code
            == 401
        )


def test_handler_failure_is_not_acknowledged(settings):
    async def failed(event):
        raise RuntimeError("PRIVATE_HANDLER_FAILURE")

    with TestClient(
        create_app(settings, EventDispatcher({"APPLICATION_CREATED": failed}))
    ) as client:
        event = {
            "type": "APPLICATION_CREATED",
            "payload": EVENTS["APPLICATION_CREATED"],
        }
        response = client.post(URL, json=event, headers=event_headers("application"))
        assert response.status_code == 503
        assert response.json() == {"detail": "Event handler failed."}
