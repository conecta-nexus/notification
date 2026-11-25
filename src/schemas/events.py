from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ApplicationCreated(EventPayload):
    student_id: UUID
    challenge_id: UUID
    company_id: UUID


class ApplicationAccepted(EventPayload):
    application_id: UUID
    student_id: UUID
    company_id: UUID
    contacts: dict[str, JsonValue]


class ApplicationRejected(EventPayload):
    application_id: UUID
    student_id: UUID
    feedback_reason: Annotated[
        str, StringConstraints(min_length=1, strip_whitespace=True)
    ]


class ChallengeClosed(EventPayload):
    """Provisional contract pending a Challenge producer specification."""

    challenge_id: UUID


class CreatedEvent(EventPayload):
    type: Literal["APPLICATION_CREATED"]
    payload: ApplicationCreated


class AcceptedEvent(EventPayload):
    type: Literal["APPLICATION_ACCEPTED"]
    payload: ApplicationAccepted


class RejectedEvent(EventPayload):
    type: Literal["APPLICATION_REJECTED"]
    payload: ApplicationRejected


class ClosedEvent(EventPayload):
    type: Literal["CHALLENGE_CLOSED"]
    payload: ChallengeClosed


Event = Annotated[
    CreatedEvent | AcceptedEvent | RejectedEvent | ClosedEvent,
    Field(discriminator="type"),
]
EventType = Literal[
    "APPLICATION_CREATED",
    "APPLICATION_ACCEPTED",
    "APPLICATION_REJECTED",
    "CHALLENGE_CLOSED",
]
