import asyncio
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPDataError,
    SMTPReadTimeoutError,
    SMTPRecipientRefused,
)
from pydantic import ValidationError

from src.core.config import Settings
from src.schemas.events import AcceptedEvent
from src.services.application_email import (
    PartialDeliveryError,
    application_email_handlers,
)
from src.services.email_rendering import (
    AcceptedEmail,
    CounterpartyContact,
    CreatedEmail,
    RejectedEmail,
    build_message,
)
from src.services.email_service import DeliveryError, DeliveryReceipt, EmailDispatcher

STUDENT = "student@example.org"
COMPANY = "company@example.org"
SENDER = "notification@example.org"
TITLE = "Automatización y análisis"


def settings(**overrides):
    defaults = {
        "database_url": "postgresql://localhost/test",
        "smtp_host": "localhost",
        "smtp_from_email": SENDER,
        "frontend_base_url": "https://platform.example.org",
    }
    return Settings(_env_file=None, **(defaults | overrides))


def created():
    return CreatedEmail(recipient=COMPANY, challenge_title=TITLE)


def accepted(role):
    return AcceptedEmail(
        recipient=STUDENT if role == "student" else COMPANY,
        challenge_title=TITLE,
        recipient_role=role,
        counterparty=CounterpartyContact(
            name="Contraparte",
            email=COMPANY if role == "student" else STUDENT,
        ),
    )


@pytest.mark.parametrize(
    ("context", "recipient", "subject", "route", "detail"),
    [
        (
            created(),
            COMPANY,
            "Recibiste una nueva postulación",
            "/challenges/manage",
            "gestión de tus retos",
        ),
        (accepted("student"), STUDENT, "Tu postulación fue aceptada", "/me", COMPANY),
        (
            accepted("company"),
            COMPANY,
            "Confirmación de postulación aceptada",
            "/challenges/manage",
            STUDENT,
        ),
        (
            RejectedEmail(
                recipient=STUDENT,
                challenge_title=TITLE,
                feedback="Técnica clara\nFaltó práctica",
            ),
            STUDENT,
            "Decisión sobre tu postulación",
            "/challenges",
            "Técnica clara\nFaltó práctica",
        ),
    ],
)
def test_mime_for_each_recipient(context, recipient, subject, route, detail):
    message = BytesParser(policy=policy.default).parsebytes(
        build_message(settings(), context).as_bytes(policy=policy.SMTP)
    )
    assert message["From"].addresses[0].addr_spec == SENDER
    assert [addr.addr_spec for addr in message["To"].addresses] == [recipient]
    assert message["Cc"] is None
    assert str(message["Subject"]) == subject
    assert message["Date"] and message["Message-ID"]
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    url = "https://platform.example.org" + route
    for part in message.iter_parts():
        content = part.get_content().replace("\r\n", "\n")
        assert TITLE in content and url in content and detail in content
        assert recipient not in content


def test_untrusted_text_is_escaped_and_optional_content_graceful():
    item = RejectedEmail(
        recipient=STUDENT,
        challenge_title="<h1>{{ 7*7 }}</h1>",
        feedback="<script>private</script>",
    )
    message = build_message(settings(), item)
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "{{ 7*7 }}" in html and "&lt;h1&gt;" in html
    assert (
        "Estudiante:"
        not in build_message(settings(), created()).get_body().get_content()
    )
    item = RejectedEmail(recipient=STUDENT, challenge_title=TITLE)
    assert (
        "Comentarios de la empresa"
        not in build_message(settings(), item).get_body().get_content()
    )
    with pytest.raises(ValidationError):
        CreatedEmail(recipient=COMPANY)
    with pytest.raises(ValidationError):
        CounterpartyContact(name="Sin método")
    with pytest.raises(ValidationError):
        AcceptedEmail(
            recipient=STUDENT,
            challenge_title=TITLE,
            recipient_role="student",
            counterparty=CounterpartyContact(name="El mismo", email=STUDENT),
        )


@pytest.mark.parametrize(
    "address",
    [
        "invalid",
        "other@example.org\r\nBcc: outsider@example.org",
        "Alice <person@example.org>",
    ],
)
def test_unsafe_mailbox(address):
    with pytest.raises(ValidationError):
        CreatedEmail(recipient=address, challenge_title=TITLE)
    with pytest.raises(ValidationError):
        settings(smtp_from_email=address)


@pytest.mark.parametrize(
    "value",
    [
        "javascript:do()",
        "//external.example.org",
        "data:text/plain,x",
        "https://user:password@external.example.org",
        "https://platform.example.org/redirect?next=external",
    ],
)
def test_unsafe_frontend_origin(value):
    with pytest.raises(ValidationError):
        settings(frontend_base_url=value)


@pytest.mark.parametrize(
    "config",
    [
        {"smtp_tls_mode": "tls,starttls"},
        {"app_env": "production"},
        {"smtp_username": "synthetic-secret"},
        {"smtp_timeout": float("inf")},
        {"smtp_retry_base_delay": 0},
        {"smtp_port": 65536},
        {"smtp_from_name": "Sender\r\nTo:"},
    ],
)
def test_invalid_transport_configuration(config):
    with pytest.raises(ValidationError) as failure:
        settings(**config)
    assert "synthetic-secret" not in str(failure.value)


def test_empty_environment_disables_dotenv_transport(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DATABASE_URL=postgresql://localhost/test\nSMTP_HOST=mailpit\nSMTP_USERNAME=legacy-user\nSMTP_PASSWORD=legacy-password\n",
        encoding="ascii",
    )
    for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.setenv(key, "")
    loaded = Settings(_env_file=dotenv)
    assert loaded.smtp_host is loaded.smtp_username is loaded.smtp_password is None


@pytest.fixture
def transport(monkeypatch):
    smtp = MagicMock()
    for name in ("connect", "mail", "rcpt", "data"):
        setattr(smtp, name, AsyncMock())
    constructor = MagicMock(return_value=smtp)
    sleep = AsyncMock()
    monkeypatch.setattr("src.services.email_service.AsyncSMTP", constructor)
    monkeypatch.setattr("src.services.email_service.asyncio.sleep", sleep)
    return smtp, constructor, sleep


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "starttls", "tls"])
async def test_config_forwarding_and_first_success(transport, mode):
    smtp, constructor, sleep = transport
    authentication = (
        {}
        if mode == "plain"
        else {"smtp_username": "fixture", "smtp_password": "synthetic"}
    )
    config = settings(
        smtp_tls_mode=mode, smtp_port=2525, smtp_timeout=2, **authentication
    )
    receipt = await EmailDispatcher(config).send(created())
    assert receipt.attempts == 1
    constructor.assert_called_once_with(
        hostname="localhost",
        port=2525,
        timeout=2,
        username=authentication.get("smtp_username"),
        password=authentication.get("smtp_password"),
        validate_certs=True,
        start_tls=mode == "starttls",
        use_tls=mode == "tls",
    )
    smtp.mail.assert_awaited_once_with(SENDER)
    smtp.rcpt.assert_awaited_once_with(COMPANY)
    parsed = BytesParser(policy=policy.default).parsebytes(smtp.data.await_args.args[0])
    assert parsed["Message-ID"] == receipt.message_id
    smtp.close.assert_called_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_explicit_transient_smtp_reply_backoff_and_stable_id(
    transport, failures, caplog
):
    smtp, _, sleep = transport
    smtp.data.side_effect = [SMTPDataError(451, "PRIVATE SMTP response")] * failures + [
        None
    ]
    dispatcher = EmailDispatcher(settings())
    if failures == 3:
        with pytest.raises(DeliveryError) as failure:
            await dispatcher.send(created())
        assert (failure.value.reason, failure.value.attempts) == ("temporary", 3)
        assert "reason=temporary attempts=3" in caplog.text
    else:
        assert (await dispatcher.send(created())).attempts == failures + 1
    assert smtp.data.await_count == min(failures + 1, 3)
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2][:failures]
    messages = [
        BytesParser(policy=policy.default).parsebytes(call.args[0])
        for call in smtp.data.await_args_list
    ]
    assert len({str(message["Message-ID"]) for message in messages}) == 1
    assert "PRIVATE" not in caplog.text and COMPANY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "error", "reason"),
    [
        ("rcpt", SMTPRecipientRefused(550, "PRIVATE", COMPANY), "permanent"),
        ("connect", SMTPAuthenticationError(535, "PRIVATE"), "permanent"),
        ("data", SMTPReadTimeoutError("PRIVATE"), "ambiguous"),
    ],
)
async def test_terminal_errors_never_retry(transport, stage, error, reason, caplog):
    smtp, _, sleep = transport
    getattr(smtp, stage).side_effect = error
    with pytest.raises(DeliveryError) as failure:
        await EmailDispatcher(settings()).send(created())
    assert (failure.value.reason, failure.value.attempts) == (reason, 1)
    assert "PRIVATE" not in str(failure.value) + caplog.text
    smtp.close.assert_called_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_failures_retry_but_cancellation_propagates(transport):
    smtp, _, sleep = transport
    smtp.connect.side_effect = [SMTPConnectError("unavailable"), TimeoutError(), None]
    assert (await EmailDispatcher(settings()).send(created())).attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]
    smtp.connect.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await EmailDispatcher(settings()).send(created())
    assert smtp.close.call_count == 4


@pytest.mark.asyncio
async def test_disabled_or_incomplete_delivery_fails_without_smtp(transport):
    _, constructor, _ = transport
    for config, reason in [
        (settings(smtp_host=None), "disabled"),
        (settings(smtp_from_email=None), "configuration"),
    ]:
        with pytest.raises(DeliveryError) as failure:
            await EmailDispatcher(config).send(created())
        assert (failure.value.reason, failure.value.attempts) == (reason, 0)
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_paired_send_returns_partial_failure_without_resending_success():
    mailbox = uuid4()
    event = AcceptedEvent(
        type="APPLICATION_ACCEPTED",
        payload={
            "application_id": mailbox,
            "student_id": mailbox,
            "company_id": mailbox,
            "contacts": {},
        },
    )
    sender = MagicMock()
    sender.send = AsyncMock(
        side_effect=[
            DeliveryReceipt("<stable@example.org>", 1),
            DeliveryError("permanent", 1),
        ]
    )
    inputs = [accepted("student"), accepted("company")]
    handler = application_email_handlers(sender, AsyncMock(return_value=inputs))[
        event.type
    ]
    with pytest.raises(PartialDeliveryError) as failure:
        await handler(event)
    assert [result.role for result in failure.value.results] == ["student", "company"]
    assert [type(result.outcome) for result in failure.value.results] == [
        DeliveryReceipt,
        DeliveryError,
    ]
    assert [call.args[0] for call in sender.send.await_args_list] == inputs
    malformed = application_email_handlers(sender, AsyncMock(return_value=inputs[:1]))[
        event.type
    ]
    with pytest.raises(ValueError):
        await malformed(event)
    assert sender.send.await_count == 2
