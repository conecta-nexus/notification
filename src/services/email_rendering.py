from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    StringConstraints,
    model_validator,
)

from src.core.config import Settings


def mailbox(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or any(ch.isspace() or ch in "<>" for ch in value)
    ):
        raise ValueError("Provide one plain ASCII email address.")
    return value


Mailbox = Annotated[EmailStr, BeforeValidator(mailbox)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EmailContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    recipient: Mailbox
    challenge_title: Text
    delivery_id: UUID | None = None


class CreatedEmail(EmailContext):
    student_name: Text | None = None


class CounterpartyContact(BaseModel):
    model_config = EmailContext.model_config
    name: Text
    email: Mailbox | None = None
    phone: Text | None = None

    @model_validator(mode="after")
    def require_contact(self):
        if not self.email and not self.phone:
            raise ValueError("An authorized contact method is required.")
        return self


class AcceptedEmail(EmailContext):
    recipient_role: Literal["student", "company"]
    counterparty: CounterpartyContact

    @model_validator(mode="after")
    def exclude_self_contact(self):
        if (
            self.counterparty.email
            and self.counterparty.email.casefold() == self.recipient.casefold()
        ):
            raise ValueError("The contact email must belong to the counterparty.")
        return self


class RejectedEmail(EmailContext):
    feedback: Text | None = None


TransactionalEmail = CreatedEmail | AcceptedEmail | RejectedEmail
TEMPLATES = {
    CreatedEmail: (
        "application_created",
        "Recibiste una nueva postulación",
        "/challenges/manage",
        "Ir a mis retos",
    ),
    AcceptedEmail: (
        "application_accepted",
        "Tu postulación fue aceptada",
        "/me",
        "Ir a mi perfil",
    ),
    RejectedEmail: (
        "application_rejected",
        "Decisión sobre tu postulación",
        "/challenges",
        "Explorar retos",
    ),
}
JINJA = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=select_autoescape(("html",), default_for_string=True),
    undefined=StrictUndefined,
)


def build_message(settings: Settings, context: TransactionalEmail) -> EmailMessage:
    if not (sender := settings.smtp_from_email) or not settings.frontend_base_url:
        raise ValueError(
            "Configure a sender and frontend origin before rendering email."
        )
    template, subject, route, action = TEMPLATES[type(context)]
    if isinstance(context, AcceptedEmail) and context.recipient_role == "company":
        subject, route, action = (
            "Confirmación de postulación aceptada",
            "/challenges/manage",
            "Ir a mis retos",
        )
    origin = str(settings.frontend_base_url).rstrip("/")
    values = {
        "email": context,
        "subject": subject,
        "action_url": origin + route,
        "action_label": action,
    }
    message = EmailMessage()
    message["From"] = Address(display_name=settings.smtp_from_name, addr_spec=sender)
    message["To"] = Address(addr_spec=context.recipient)
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    domain = sender.rsplit("@", 1)[1]
    message["Message-ID"] = (
        f"<{context.delivery_id}@{domain}>"
        if context.delivery_id
        else make_msgid(domain=domain)
    )
    message.set_content(
        JINJA.get_template(template + ".txt").render(values),
        charset="utf-8",
        cte="quoted-printable",
    )
    message.add_alternative(
        JINJA.get_template(template + ".html").render(values),
        subtype="html",
        charset="utf-8",
        cte="quoted-printable",
    )
    return message
