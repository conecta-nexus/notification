import asyncio
import ssl
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPDataError,
    SMTPException,
    SMTPReadTimeoutError,
    SMTPRecipientRefused,
    SMTPSenderRefused,
)
from jinja2 import UndefinedError
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "context", "recipient", "subject", "route", "required", "forbidden"),
    [
        (
            "created-company",
            created(),
            COMPANY,
            "Recibiste una nueva postulación",
            "/challenges/manage",
            "gestión de tus retos",
            STUDENT,
        ),
        (
            "accepted-student",
            accepted("student"),
            STUDENT,
            "Tu postulación fue aceptada",
            "/me",
            COMPANY,
            STUDENT,
        ),
        (
            "accepted-company",
            accepted("company"),
            COMPANY,
            "Confirmación de postulación aceptada",
            "/challenges/manage",
            STUDENT,
            COMPANY,
        ),
        (
            "rejected-student",
            RejectedEmail(
                recipient=STUDENT,
                challenge_title=TITLE,
                feedback="Agradecemos tu interés.\n"
                + "Detalles técnicos de la evaluación. " * 20,
            ),
            STUDENT,
            "Decisión sobre tu postulación",
            "/challenges",
            "Detalles técnicos",
            COMPANY,
        ),
    ],
)
async def test_smtp_data_bytes_for_each_recipient(
    transport,
    name,
    context,
    recipient,
    subject,
    route,
    required,
    forbidden,
):
    smtp, constructor, delay = transport
    dispatcher = EmailDispatcher(settings(smtp_port=1025))
    receipt = await dispatcher.send(context)
    assert receipt.attempts == 1
    assert constructor.call_args.kwargs == {
        "hostname": "localhost",
        "port": 1025,
        "timeout": 10,
        "username": None,
        "password": None,
        "use_tls": False,
        "start_tls": False,
        "validate_certs": True,
    }
    smtp.connect.assert_awaited_once()
    smtp.mail.assert_awaited_once_with(SENDER)
    smtp.rcpt.assert_awaited_once_with(recipient)
    smtp.data.assert_awaited_once()
    payload = smtp.data.await_args.args[0]
    assert isinstance(payload, bytes)
    message = BytesParser(policy=policy.default).parsebytes(payload)
    assert [address.addr_spec for address in message["From"].addresses] == [SENDER]
    assert [address.addr_spec for address in message["To"].addresses] == [recipient]
    assert message["Date"].datetime.utcoffset() is not None
    assert str(message["Subject"]) == subject
    assert str(message["Message-ID"]) == receipt.message_id
    assert message["Cc"] is None and message["Bcc"] is None
    assert message.get_content_type() == "multipart/alternative"
    parts = list(message.iter_parts())
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    url = "https://platform.example.org" + route
    for part in parts:
        body = part.get_content()
        assert TITLE in body and url in body and required in body
        assert forbidden not in body
    assert f'href="{url}"' in parts[1].get_content()
    smtp.close.assert_called_once()
    delay.assert_not_awaited()
    # The verification artifact is opt-in and contains only these reserved-domain fixtures.
    import os
    from pathlib import Path

    if directory := os.getenv("NOTIFICATION_TEST_MIME_DIR"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.eml").write_bytes(payload)


def test_existing_template_environment_rejects_undefined_context():
    from src.services.email_rendering import JINJA

    with pytest.raises(UndefinedError):
        JINJA.get_template("application_created.html").render(
            subject="Synthetic",
            action_url="https://platform.example.org",
            action_label="Open",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_certificate_failure_never_retries(transport, wrapped, caplog):
    smtp, _, delay = transport
    certificate = ssl.SSLCertVerificationError("synthetic private certificate detail")
    error = (
        SMTPConnectError("synthetic private connection detail")
        if wrapped
        else certificate
    )
    if wrapped:
        error.__cause__ = certificate
    smtp.connect.side_effect = error
    with pytest.raises(DeliveryError) as caught:
        await EmailDispatcher(settings(smtp_tls_mode="starttls")).send(created())
    assert (caught.value.reason, caught.value.attempts) == ("permanent", 1)
    assert "synthetic private" not in caplog.text + str(caught.value)
    smtp.data.assert_not_awaited()
    smtp.close.assert_called_once()
    delay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "exception", "reason"),
    [
        ("connect", SMTPException("synthetic private SMTP context"), "permanent"),
        ("data", SMTPException("synthetic private DATA result"), "ambiguous"),
        (
            "mail",
            SMTPSenderRefused(550, "synthetic private SMTP reply", SENDER),
            "permanent",
        ),
    ],
)
async def test_transport_boundary_preserves_failure_classification(
    transport, caplog, stage, exception, reason
):
    smtp, _, delay = transport
    getattr(smtp, stage).side_effect = exception
    with pytest.raises(DeliveryError) as caught:
        await EmailDispatcher(settings()).send(created())
    assert (caught.value.reason, caught.value.attempts) == (reason, 1)
    assert "synthetic private" not in caplog.text + str(caught.value)
    smtp.close.assert_called_once()
    delay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["data", "backoff"])
async def test_cancellation_during_data_or_backoff_never_resends(transport, stage):
    smtp, _, delay = transport
    if stage == "data":
        smtp.data.side_effect = asyncio.CancelledError()
    else:
        smtp.connect.side_effect = SMTPConnectError("Temporary synthetic failure")
        delay.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await EmailDispatcher(settings()).send(created())
    smtp.connect.assert_awaited_once()
    smtp.close.assert_called_once()
    assert smtp.data.await_count == (1 if stage == "data" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["wrong-class", "empty", "duplicate-role", "duplicate-mailbox", "resolver-error"],
)
async def test_accepted_delivery_validates_entire_recipient_set_before_sending(
    scenario,
):
    base = [accepted("student"), accepted("company")]
    cases = {
        "wrong-class": [created(), base[1]],
        "empty": [],
        "duplicate-role": [base[0], base[0]],
        "duplicate-mailbox": [
            base[0],
            base[1].model_copy(update={"recipient": STUDENT}),
        ],
    }
    dispatcher = MagicMock()
    dispatcher.send = AsyncMock()
    resolver = (
        AsyncMock(side_effect=LookupError("Synthetic resolver failure"))
        if scenario == "resolver-error"
        else AsyncMock(return_value=cases[scenario])
    )
    user = uuid4()
    event = AcceptedEvent(
        type="APPLICATION_ACCEPTED",
        payload={
            "application_id": user,
            "student_id": user,
            "company_id": user,
            "contacts": {},
        },
    )
    handler = application_email_handlers(dispatcher, resolver)[event.type]
    with pytest.raises(LookupError if scenario == "resolver-error" else ValueError):
        await handler(event)
    resolver.assert_awaited_once_with(event)
    dispatcher.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_roles", [(), ("student",), ("student", "company")])
async def test_accepted_handler_keeps_independent_delivery_outcomes(failed_roles):
    contexts = [accepted("student"), accepted("company")]
    outcomes = [
        DeliveryError("permanent", 1)
        if item.recipient_role in failed_roles
        else DeliveryReceipt(f"<{item.recipient_role}@example.org>", 1)
        for item in contexts
    ]
    dispatcher = MagicMock()
    dispatcher.send = AsyncMock(side_effect=outcomes)
    user = uuid4()
    event = AcceptedEvent(
        type="APPLICATION_ACCEPTED",
        payload={
            "application_id": user,
            "student_id": user,
            "company_id": user,
            "contacts": {},
        },
    )
    handler = application_email_handlers(dispatcher, AsyncMock(return_value=contexts))[
        event.type
    ]
    if failed_roles:
        with pytest.raises(PartialDeliveryError) as error:
            await handler(event)
        assert [
            (result.role, result.outcome) for result in error.value.results
        ] == list(
            zip(
                ("student", "company"),
                outcomes,
                strict=True,
            )
        )
    else:
        assert await handler(event) is None
    assert [call.args[0] for call in dispatcher.send.await_args_list] == contexts


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
