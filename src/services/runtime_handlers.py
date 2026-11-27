import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import Settings
from src.models.notification import Notification
from src.schemas.events import Event
from src.services.email_rendering import AcceptedEmail, CounterpartyContact
from src.services.email_service import DeliveryError, EmailDispatcher
from src.services.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _build_notification_rows(event: Event) -> list[Notification]:
    if event.type == "APPLICATION_CREATED":
        return [
            Notification(
                user_id=event.payload.company_id,
                event_type=event.type,
                title="Nueva postulación recibida",
                message="Un estudiante se ha postulado a uno de tus retos publicados.",
                link_url="/challenges/manage",
            )
        ]

    if event.type == "APPLICATION_ACCEPTED":
        return [
            Notification(
                user_id=event.payload.student_id,
                event_type=event.type,
                title="¡Tu postulación fue aceptada!",
                message="La empresa aceptó tu postulación. Ya puedes consultar sus datos de contacto.",
                link_url="/me",
            ),
            Notification(
                user_id=event.payload.company_id,
                event_type=event.type,
                title="Postulación aceptada",
                message="Confirmaste la selección académica. Los datos de contacto ya están disponibles.",
                link_url="/challenges/manage",
            ),
        ]

    if event.type == "APPLICATION_REJECTED":
        return [
            Notification(
                user_id=event.payload.student_id,
                event_type=event.type,
                title="Decisión sobre tu postulación",
                message=f"Tu postulación no fue seleccionada: {event.payload.feedback_reason}",
                link_url="/challenges",
            )
        ]

    return []


async def _dispatch_accepted_emails(
    settings: Settings, dispatcher: EmailDispatcher, event: Event
) -> None:
    if event.type != "APPLICATION_ACCEPTED":
        return
    if not (
        settings.smtp_host
        and settings.smtp_from_email
        and settings.frontend_base_url
    ):
        return

    contacts = event.payload.contacts
    student_email = _str_or_none(contacts.get("student_email"))
    company_email = _str_or_none(contacts.get("company_email"))
    student_name = _str_or_none(contacts.get("student_name")) or "Estudiante UFPS"
    company_name = _str_or_none(contacts.get("company_name")) or "Empresa Aliada"
    student_phone = _str_or_none(contacts.get("student_phone"))
    company_phone = _str_or_none(contacts.get("company_phone"))
    challenge_title = _str_or_none(contacts.get("challenge_title")) or "Reto Conecta Nexus"

    if student_email and (company_email or company_phone):
        try:
            await dispatcher.send(
                AcceptedEmail(
                    recipient=student_email,
                    recipient_role="student",
                    challenge_title=challenge_title,
                    counterparty=CounterpartyContact(
                        name=company_name,
                        email=company_email,
                        phone=company_phone,
                    ),
                )
            )
        except (DeliveryError, ValueError) as exc:
            logger.warning("Failed sending accepted email to student: %s", exc)

    if company_email and (student_email or student_phone):
        try:
            await dispatcher.send(
                AcceptedEmail(
                    recipient=company_email,
                    recipient_role="company",
                    challenge_title=challenge_title,
                    counterparty=CounterpartyContact(
                        name=student_name,
                        email=student_email,
                        phone=student_phone,
                    ),
                )
            )
        except (DeliveryError, ValueError) as exc:
            logger.warning("Failed sending accepted email to company: %s", exc)


def build_runtime_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    email_dispatcher: EmailDispatcher | None = None,
) -> EventDispatcher:
    """Build default runtime EventDispatcher persisting in-app notifications and sending SMTP emails."""
    mailer = email_dispatcher or EmailDispatcher(settings)

    async def handle_event(event: Event) -> None:
        rows = _build_notification_rows(event)
        if rows:
            async with session_factory() as session, session.begin():
                session.add_all(rows)
        await _dispatch_accepted_emails(settings, mailer, event)

    return EventDispatcher(
        {
            "APPLICATION_CREATED": handle_event,
            "APPLICATION_ACCEPTED": handle_event,
            "APPLICATION_REJECTED": handle_event,
            "CHALLENGE_CLOSED": handle_event,
        }
    )
