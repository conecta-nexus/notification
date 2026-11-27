"""Send only synthetic messages through an isolated loopback Mailpit."""

import asyncio
import json
import socket
import sys
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from secrets import token_hex
from time import monotonic
from uuid import uuid4

import httpx
import uvicorn

from src.core.config import Settings
from src.main import create_app
from src.services.application_email import application_email_handlers
from src.services.email_rendering import (
    AcceptedEmail,
    CounterpartyContact,
    CreatedEmail,
    RejectedEmail,
)
from src.services.email_service import DeliveryError, EmailDispatcher
from src.services.event_dispatcher import EventDispatcher

HOST = "127.0.0.1"
SMTP_PORT = 1025
MAILPIT = "http://127.0.0.1:8025"
SENDER = "notification@example.org"
STUDENT = "student@example.org"
COMPANY = "company@example.org"


def now():
    return datetime.now(UTC).isoformat()


def socket_on_loopback():
    sock = socket.socket()
    sock.bind((HOST, 0))
    return sock


async def proxy(reader, writer):
    upstream, upstream_writer = await asyncio.open_connection(HOST, SMTP_PORT)

    async def pipe(source, destination):
        while data := await source.read(65536):
            destination.write(data)
            await destination.drain()

    tasks = [
        asyncio.create_task(pipe(reader, upstream_writer)),
        asyncio.create_task(pipe(upstream, writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        upstream_writer.close()
        writer.close()
        await asyncio.gather(
            upstream_writer.wait_closed(), writer.wait_closed(), return_exceptions=True
        )


async def verify_retry(config, marker):
    context = CreatedEmail(
        recipient=COMPANY, challenge_title=f"Reintento {marker}", delivery_id=uuid4()
    )
    available = False

    async def fault_proxy(reader, writer):
        if available:
            await proxy(reader, writer)
        else:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(fault_proxy, HOST, 0) as server:
        smtp_port = server.sockets[0].getsockname()[1]
        start = monotonic()
        started_at = now()
        first_attempt = asyncio.Event()
        attempt_results = []

        class ObservedDispatcher(EmailDispatcher):
            async def _attempt(self, message, recipient):
                result = await super()._attempt(message, recipient)
                attempt_results.append(
                    {"result": result or "accepted", "observed_at": now()}
                )
                first_attempt.set()
                return result

        delivery = asyncio.create_task(
            ObservedDispatcher(
                config.model_copy(
                    update={
                        "smtp_port": smtp_port,
                        "smtp_retry_base_delay": 0.65,
                    }
                )
            ).send(context)
        )
        try:
            async with asyncio.timeout(5):
                await first_attempt.wait()
            assert attempt_results[0]["result"] == "temporary"
            available = True
            available_at = now()
            receipt = await delivery
        finally:
            if not delivery.done():
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
        assert receipt.attempts == 2, attempt_results
        recovered = {
            "attempts": 2,
            "seconds": round(monotonic() - start, 3),
            "started_at": started_at,
            "smtp_available_at": available_at,
            "accepted_at": now(),
            "message_id": receipt.message_id,
            "attempt_results": attempt_results,
        }

    with socket_on_loopback() as sock:
        terminal = context.model_copy(update={"delivery_id": uuid4()})
        try:
            await EmailDispatcher(
                config.model_copy(
                    update={
                        "smtp_port": sock.getsockname()[1],
                        "smtp_retry_base_delay": 0.08,
                    }
                )
            ).send(terminal)
        except DeliveryError as error:
            assert (error.reason, error.attempts) == ("temporary", 3)
            failed = {
                "reason": error.reason,
                "attempts": error.attempts,
                "message_id": f"<{terminal.delivery_id}@example.org>",
            }
        else:
            raise AssertionError("Unavailable transport incorrectly succeeded")
    return recovered, failed


async def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    marker = uuid4().hex[:8]
    producer_secret = token_hex(32)
    title = f"Automatización de procesos · Prueba {marker}"
    config = Settings(
        _env_file=None,
        database_url="postgresql://127.0.0.1:1/email_verify",
        app_env="testing",
        smtp_host=HOST,
        smtp_port=SMTP_PORT,
        smtp_from_email=SENDER,
        smtp_tls_mode="plain",
        smtp_username=None,
        smtp_password=None,
        smtp_timeout=3,
        frontend_base_url="http://localhost:3000",
        application_event_secret=producer_secret,
        challenge_event_secret=None,
        jwt_public_key="",
        jwt_public_key_path=output / "unused.pem",
    )
    common = {"challenge_title": title}
    variants = {
        "created-company": CreatedEmail(
            recipient=COMPANY,
            delivery_id=uuid4(),
            student_name="Estudiante de prueba",
            **common,
        ),
        "accepted-student": AcceptedEmail(
            recipient=STUDENT,
            delivery_id=uuid4(),
            recipient_role="student",
            counterparty=CounterpartyContact(
                name="Empresa de prueba",
                email="contacto-operativo-institucional@example.org",
                phone="+57 000 000 0000",
            ),
            **common,
        ),
        "accepted-company": AcceptedEmail(
            recipient=COMPANY,
            delivery_id=uuid4(),
            recipient_role="company",
            counterparty=CounterpartyContact(
                name="Estudiante de prueba", email="contacto-estudiante@example.org"
            ),
            **common,
        ),
        "rejected-student": RejectedEmail(
            recipient=STUDENT,
            delivery_id=uuid4(),
            feedback="Agradecemos tu interés y la claridad de la propuesta.\n\n"
            + "En esta oportunidad priorizamos experiencia en automatización y documentación técnica. "
            * 3,
            **common,
        ),
    }
    contexts = {
        "APPLICATION_CREATED": [variants["created-company"]],
        "APPLICATION_ACCEPTED": [
            variants["accepted-student"],
            variants["accepted-company"],
        ],
        "APPLICATION_REJECTED": [variants["rejected-student"]],
    }
    sample_id = str(uuid4())
    payloads = {
        "APPLICATION_CREATED": {
            "student_id": sample_id,
            "challenge_id": sample_id,
            "company_id": sample_id,
        },
        "APPLICATION_ACCEPTED": {
            "application_id": sample_id,
            "student_id": sample_id,
            "company_id": sample_id,
            "contacts": {},
        },
        "APPLICATION_REJECTED": {
            "application_id": sample_id,
            "student_id": sample_id,
            "feedback_reason": "Contexto de prueba",
        },
    }
    timings = {}

    async def resolver(event):
        assert str(event.payload.student_id) == sample_id
        return contexts[event.type]

    app = create_app(
        config,
        EventDispatcher(application_email_handlers(EmailDispatcher(config), resolver)),
    )
    async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
        (await http.get(MAILPIT + "/api/v1/messages")).raise_for_status()
        with socket_on_loopback() as sock:
            url = f"http://{HOST}:{sock.getsockname()[1]}/api/v1/notifications/events"
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="warning", loop="asyncio")
            )
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("HTTP verification server did not start")
                        await asyncio.sleep(0.02)
                for kind, payload in payloads.items():
                    body = {"type": kind, "payload": payload}
                    if not timings:
                        assert (await http.post(url, json=body)).status_code == 401
                    started_at = now()
                    response = await http.post(
                        url,
                        json=body,
                        headers={
                            "X-Event-Producer": "application",
                            "X-Event-Producer-Token": producer_secret,
                        },
                    )
                    assert (
                        response.status_code == 200
                        and response.json()["status"] == "handled"
                    )
                    timings[kind] = {
                        "status": 200,
                        "started_at": started_at,
                        "accepted_at": now(),
                    }
            finally:
                server.should_exit = True
                await task
        recovered, failed = await verify_retry(config, marker)
        messages = (await http.get(MAILPIT + "/api/v1/messages")).json()["messages"]
        matching = {
            f"{context.delivery_id}@example.org": name
            for name, context in variants.items()
        }
        retry_id = recovered["message_id"].strip("<>")
        assert (
            sum(message["MessageID"].strip("<>") == retry_id for message in messages)
            == 1
        )
        assert not any(
            message["MessageID"].strip("<>") == failed["message_id"].strip("<>")
            for message in messages
        )
        captures = {}
        for entry in messages:
            if (name := matching.get(entry["MessageID"].strip("<>"))) is None:
                continue
            identifier = entry["ID"]
            raw = (
                (await http.get(f"{MAILPIT}/api/v1/message/{identifier}/raw"))
                .raise_for_status()
                .content
            )
            summary = (
                (await http.get(f"{MAILPIT}/api/v1/message/{identifier}"))
                .raise_for_status()
                .json()
            )
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            parts = list(parsed.iter_parts())
            assert [part.get_content_type() for part in parts] == [
                "text/plain",
                "text/html",
            ]
            assert [entry.addr_spec for entry in parsed["To"].addresses] == [
                variants[name].recipient
            ]
            assert (
                parsed["From"].addresses[0].addr_spec == SENDER and parsed["Cc"] is None
            )
            plain, html = [part.get_content() for part in parts]
            assert summary["Text"].strip() == plain.strip()
            assert "Conecta Nexus" in plain and marker in html and "<img" not in html
            route = (
                "/me"
                if name == "accepted-student"
                else "/challenges"
                if name == "rejected-student"
                else "/challenges/manage"
            )
            assert 'href="http://localhost:3000' + route + '"' in html
            if isinstance(variants[name], AcceptedEmail):
                for content in (plain, html):
                    assert (
                        variants[name].counterparty.email in content
                        and variants[name].recipient not in content
                    )
            (output / f"{name}.eml").write_bytes(raw)
            (output / f"{name}.txt").write_text(plain, encoding="utf-8")
            (output / f"{name}.html").write_text(html, encoding="utf-8")
            captures[name] = {
                "mailpit_id": identifier,
                "message_id": str(parsed["Message-ID"]),
                "subject": str(parsed["Subject"]),
                "smtp_date": str(parsed["Date"]),
                "mailpit_created": entry["Created"],
                "captured_at": now(),
                "sender": SENDER,
                "recipient": variants[name].recipient,
                "path": route,
                "parts": [part.get_content_type() for part in parts],
            }
        assert set(captures) == set(variants)
        result = {
            "marker": marker,
            "http": timings,
            "anonymous_status": 401,
            "mailpit": captures,
            "temporary_failure_recovery": recovered,
            "terminal_failure": failed,
            "retry_capture_count": 1,
            "terminal_capture_count": 0,
        }
        (output / "mailpit-verification.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(
            "Four event-driven messages captured; controlled retry captured once and terminal failure captured none."
        )


if __name__ == "__main__":
    asyncio.run(verify(Path(sys.argv[1])))
