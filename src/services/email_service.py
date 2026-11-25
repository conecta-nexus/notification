import asyncio
import logging
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Literal

from aiosmtplib import SMTP as AsyncSMTP
from aiosmtplib.errors import SMTPException, SMTPResponseException

from src.core.config import Settings
from src.services.email_rendering import TransactionalEmail, build_message

logger = logging.getLogger(__name__)
FailureReason = Literal[
    "disabled", "configuration", "permanent", "temporary", "ambiguous"
]


class DeliveryError(Exception):
    def __init__(self, reason: FailureReason, attempts: int):
        self.reason = reason
        self.attempts = attempts
        super().__init__(f"SMTP delivery failed ({reason}).")


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: str
    attempts: int


class EmailDispatcher:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send(self, context: TransactionalEmail) -> DeliveryReceipt:
        if not self.settings.smtp_host:
            raise DeliveryError("disabled", 0)
        try:
            message = build_message(self.settings, context)
        except ValueError:
            raise DeliveryError("configuration", 0) from None
        for attempt in range(1, 4):
            reason = await self._attempt(message, context.recipient)
            if reason is None:
                return DeliveryReceipt(str(message["Message-ID"]), attempt)
            if reason != "temporary" or attempt == 3:
                logger.error(
                    "SMTP delivery failed reason=%s attempts=%d", reason, attempt
                )
                raise DeliveryError(reason, attempt)
            await asyncio.sleep(
                self.settings.smtp_retry_base_delay * 2 ** (attempt - 1)
            )
        raise AssertionError("SMTP retry limit exceeded")

    async def _attempt(
        self, message: EmailMessage, recipient: str
    ) -> FailureReason | None:
        config = self.settings
        client = AsyncSMTP(
            hostname=config.smtp_host,
            port=config.smtp_port,
            timeout=config.smtp_timeout,
            username=config.smtp_username.get_secret_value()
            if config.smtp_username
            else None,
            password=config.smtp_password.get_secret_value()
            if config.smtp_password
            else None,
            use_tls=config.smtp_tls_mode == "tls",
            start_tls=config.smtp_tls_mode == "starttls",
            validate_certs=True,
        )
        data_started = False
        try:
            async with asyncio.timeout(config.smtp_timeout):
                await client.connect()
                await client.mail(config.smtp_from_email)
                await client.rcpt(recipient)
                data_started = True
                await client.data(message.as_bytes(policy=SMTP))
        except SMTPResponseException as error:
            return "temporary" if 400 <= error.code < 500 else "permanent"
        except (OSError, TimeoutError, SMTPException) as error:
            if isinstance(error, ssl.SSLError) or isinstance(
                error.__cause__, ssl.SSLError
            ):
                return "permanent"
            if isinstance(error, (TimeoutError, OSError)):
                return "ambiguous" if data_started else "temporary"
            return "ambiguous" if data_started else "permanent"
        finally:
            client.close()
        return None
