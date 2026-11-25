from collections.abc import Awaitable, Callable, Mapping

from src.schemas.events import Event, EventType

EventHandler = Callable[[Event], Awaitable[None]]


class HandlerUnavailable(Exception):
    """The event has no configured handler."""


class HandlerFailed(Exception):
    """The configured handler did not complete."""


class EventDispatcher:
    def __init__(self, handlers: Mapping[EventType, EventHandler] | None = None):
        self._handlers = dict(handlers or {})

    async def dispatch(self, event: Event) -> None:
        handler = self._handlers.get(event.type)
        if handler is None:
            raise HandlerUnavailable
        try:
            await handler(event)
        except Exception:  # noqa: BLE001 - handler failures must never acknowledge the event
            raise HandlerFailed from None
