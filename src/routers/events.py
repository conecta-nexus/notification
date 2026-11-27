import logging
from hmac import compare_digest
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from src.schemas.events import Event
from src.services.event_dispatcher import HandlerFailed, HandlerUnavailable

router = APIRouter(prefix="/api/v1/notifications")
logger = logging.getLogger(__name__)
Producer = Literal["application", "challenge"]


def internal_producer(
    request: Request,
    x_event_producer: Annotated[str | None, Header()] = None,
    x_event_producer_token: Annotated[str | None, Header()] = None,
) -> Producer:
    settings = request.app.state.settings
    secrets = {
        "application": settings.application_event_secret,
        "challenge": settings.challenge_event_secret,
    }
    secret = secrets.get(x_event_producer)
    if (
        not secret
        or not x_event_producer_token
        or not compare_digest(
            secret.get_secret_value().encode(),
            x_event_producer_token.encode(),
        )
    ):
        raise HTTPException(401, "Invalid or missing producer credentials.")
    if secrets["application"] == secrets["challenge"]:
        raise HTTPException(401, "Invalid or missing producer credentials.")
    return x_event_producer


@router.post("/events")
async def receive_event(
    request: Request,
    producer: Annotated[Producer, Depends(internal_producer)],
    event: Event,
) -> dict[str, str]:
    expected_producer = (
        "challenge" if event.type == "CHALLENGE_CLOSED" else "application"
    )
    if producer != expected_producer:
        raise HTTPException(403, "Producer cannot submit this event type.")
    try:
        await request.app.state.dispatcher.dispatch(event)
    except HandlerUnavailable:
        raise HTTPException(503, "No event handler is available.") from None
    except HandlerFailed:
        logger.error("Notification event handler failed for %s", event.type)
        raise HTTPException(503, "Event handler failed.") from None
    return {"status": "handled", "type": event.type}
