from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from src.schemas.events import Event
from src.services.email_rendering import (
    AcceptedEmail,
    CreatedEmail,
    RejectedEmail,
    TransactionalEmail,
)
from src.services.email_service import DeliveryError, DeliveryReceipt, EmailDispatcher

Resolver = Callable[[Event], Awaitable[Sequence[TransactionalEmail]]]


@dataclass(frozen=True)
class RecipientResult:
    role: str
    outcome: DeliveryReceipt | DeliveryError


class PartialDeliveryError(Exception):
    def __init__(self, results: tuple[RecipientResult, ...]):
        self.results = results
        super().__init__("One or more application emails could not be delivered.")


def application_email_handlers(dispatcher: EmailDispatcher, resolver: Resolver):
    """Handlers for EventDispatcher using an explicitly authorized context resolver."""
    expected = {
        "APPLICATION_CREATED": (CreatedEmail, {"company"}),
        "APPLICATION_ACCEPTED": (AcceptedEmail, {"student", "company"}),
        "APPLICATION_REJECTED": (RejectedEmail, {"student"}),
    }

    async def handle(event: Event) -> None:
        contexts = await resolver(event)
        context_type, expected_roles = expected[event.type]
        if not all(isinstance(context, context_type) for context in contexts):
            raise ValueError("Invalid authorized email context.")
        roles = [
            context.recipient_role
            if isinstance(context, AcceptedEmail)
            else next(iter(expected_roles))
            for context in contexts
        ]
        if set(roles) != expected_roles or len(contexts) != len(expected_roles):
            raise ValueError("Email contexts must cover each intended recipient once.")
        if len({context.recipient.casefold() for context in contexts}) != len(contexts):
            raise ValueError("Each recipient must have a distinct mailbox.")
        results = []
        failed = False
        for role, context in zip(roles, contexts, strict=True):
            try:
                outcome = await dispatcher.send(context)
            except DeliveryError as error:
                outcome = error
                failed = True
            results.append(RecipientResult(role, outcome))
        if failed:
            raise PartialDeliveryError(tuple(results))

    return {kind: handle for kind in expected}
